"""
@author: Jinghan Gao, Baixu Chen
@contact: getterk@163.com, cbx_99_hasta@outlook.com
"""
import random
import time
import warnings
import argparse
import shutil
import os.path as osp

import numpy as np
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.optim import SGD
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torchvision.transforms as T

import utils
import tllib.vision.datasets.universal as datasets
from tllib.vision.datasets.universal import default_universal as universal
from tllib.alignment.cmu import ImageClassifier, Ensemble, get_marginal_confidence, get_entropy, norm
from tllib.modules.domain_discriminator import DomainDiscriminator
from tllib.alignment.dann import DomainAdversarialLoss
from tllib.utils.data import ForeverDataIterator
from tllib.utils.metric import accuracy, ConfusionMatrix
from tllib.utils.meter import AverageMeter, ProgressMeter
from tllib.utils.logger import CompleteLogger
from tllib.utils.analysis import collect_feature, tsne, a_distance

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

ens_transforms = [
    T.Compose([T.Resize(256),
               T.RandomHorizontalFlip(),
               T.RandomAffine(degrees=30, translate=(0.1, 0.1), scale=(0.9, 1.1), shear=0.2,
                              interpolation=T.InterpolationMode.BICUBIC, fill=(255, 255, 255)),
               T.CenterCrop(224),
               T.RandomGrayscale(p=0.5),
               T.ToTensor(),
               T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])]),
    T.Compose([T.Resize(256),
               T.RandomHorizontalFlip(),
               T.RandomPerspective(),
               T.FiveCrop(224),
               T.Lambda(lambda crops: crops[0]),
               T.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.2),
               T.ToTensor(),
               T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])]),
    T.Compose([T.Resize(256),
               T.RandomHorizontalFlip(),
               T.RandomAffine(degrees=30, translate=(0.1, 0.1), scale=(0.9, 1.1), shear=0.2,
                              interpolation=T.InterpolationMode.BICUBIC, fill=(255, 255, 255)),
               T.FiveCrop(224),
               T.Lambda(lambda crops: crops[1]),
               T.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.2),
               T.ToTensor(),
               T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])]),
    T.Compose([T.Resize(256),
               T.RandomHorizontalFlip(),
               T.RandomAffine(degrees=10, translate=(0.1, 0.1), scale=(0.9, 1.1), shear=0.1,
                              interpolation=T.InterpolationMode.BICUBIC, fill=(255, 255, 255)),
               T.RandomPerspective(),
               T.FiveCrop(224),
               T.Lambda(lambda crops: crops[2]),
               T.ToTensor(),
               T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])]),
    T.Compose([T.Resize(256),
               T.RandomHorizontalFlip(),
               T.RandomPerspective(),
               T.FiveCrop(224),
               T.Lambda(lambda crops: crops[3]),
               T.RandomGrayscale(p=0.5),
               T.ToTensor(),
               T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
]


def main(args: argparse.Namespace):
    logger = CompleteLogger(args.log, args.phase)
    print(args)

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    cudnn.benchmark = True

    # Data loading code
    train_transform = utils.get_train_transform(args.train_resizing)
    val_transform = utils.get_val_transform(args.val_resizing)
    print("train_transform: ", train_transform)
    print("val_transform: ", val_transform)

    dataset = datasets.__dict__[args.data]
    source_dataset = universal(dataset, source=True)
    target_dataset = universal(dataset, source=False)
    train_source_dataset = source_dataset(root=args.root, task=args.source, download=True, transform=train_transform)
    train_target_dataset = target_dataset(root=args.root, task=args.target, download=True, transform=train_transform)
    ens_datasets = [source_dataset(root=args.root, task=args.source, download=True, transform=ens_transforms[i]) for i
                    in range(5)]
    val_dataset = target_dataset(root=args.root, task=args.target, download=True, transform=val_transform)
    if args.data == 'DomainNet':
        test_dataset = target_dataset(root=args.root, task=args.target, split='test', download=True,
                                      transform=val_transform)
    else:
        test_dataset = val_dataset
    num_classes = train_source_dataset.num_classes
    num_common_classes = train_source_dataset.num_common_classes

    train_source_loader = DataLoader(train_source_dataset, batch_size=args.batch_size, shuffle=True,
                                     num_workers=args.workers, drop_last=True)
    train_target_loader = DataLoader(train_target_dataset, batch_size=args.batch_size, shuffle=True,
                                     num_workers=args.workers, drop_last=True)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)
    test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)

    train_source_iter = ForeverDataIterator(train_source_loader)
    train_target_iter = ForeverDataIterator(train_target_loader)
    ens_iters = [ForeverDataIterator(
        DataLoader(ens_datasets[i], batch_size=args.batch_size, shuffle=True, num_workers=args.workers, drop_last=True))
        for i in range(5)]

    # create model
    print("=> using pre-trained model '{}'".format(args.arch))
    backbone = utils.get_model(args.arch)
    pool_layer = nn.Identity() if args.no_pool else None
    classifier = ImageClassifier(backbone, num_classes, bottleneck_dim=args.bottleneck_dim, pool_layer=pool_layer,
                                 finetune=True).to(device)
    ens_classifier = Ensemble(classifier.features_dim, train_source_dataset.num_classes).to(device)

    if args.phase != 'train':
        classifier.load_state_dict(torch.load(logger.get_checkpoint_path('best_classifier')))
        ens_classifier.load_state_dict(torch.load(logger.get_checkpoint_path('best_ens_classifier')))

    # analysis the model
    if args.phase == 'analysis':
        # extract features from both domains
        feature_extractor = nn.Sequential(classifier.backbone, classifier.pool_layer, classifier.bottleneck).to(device)
        source_feature = collect_feature(train_source_loader, feature_extractor, device)
        target_feature = collect_feature(train_target_loader, feature_extractor, device)
        # plot t-SNE
        tSNE_filename = osp.join(logger.visualize_directory, 'TSNE.png')
        tsne.visualize(source_feature, target_feature, tSNE_filename)
        print("Saving t-SNE to", tSNE_filename)
        # calculate A-distance, which is a measure for distribution discrepancy
        A_distance = a_distance.calculate(source_feature, target_feature, device)
        print("A-distance =", A_distance)
        return

    if args.phase == 'test':
        acc1, h_score = validate(test_loader, classifier, ens_classifier, num_classes, num_common_classes, args)
        return

    # ==================================================================================================================
    # stage 1 pretrain the classifier and ens_classifier
    # ==================================================================================================================
    print('Stage 1: Pretraining')
    optimizer_pretrain = SGD(classifier.get_parameters() + ens_classifier.get_parameters(), args.lr,
                             momentum=args.momentum, weight_decay=args.weight_decay, nesterov=True)
    lr_lambda = lambda x: args.lr * (1. + args.lr_gamma * float(x)) ** (-args.lr_decay)
    lr_scheduler_pretrain = LambdaLR(optimizer_pretrain, lr_lambda)

    for epoch in range(args.epochs_pretrain):
        pretrain(train_source_iter, ens_iters, classifier, ens_classifier, optimizer_pretrain, lr_scheduler_pretrain,
                 epoch, args)

    # domain discriminator
    domain_discri = DomainDiscriminator(in_feature=classifier.features_dim, hidden_size=1024).to(device)
    # define optimizer and lr scheduler
    optimizer = SGD(classifier.get_parameters() + domain_discri.get_parameters(), args.lr, momentum=args.momentum,
                    weight_decay=args.weight_decay, nesterov=True)
    lr_scheduler = LambdaLR(optimizer, lr_lambda)

    ens_optimizer = SGD(ens_classifier.get_parameters(), args.lr, momentum=args.momentum,
                        weight_decay=args.weight_decay, nesterov=True)
    ens_lr_scheduler = [LambdaLR(ens_optimizer, lr_lambda)] * 5

    # define loss function
    domain_adv = DomainAdversarialLoss(domain_discri).to(device)

    # ==================================================================================================================
    # stage 2 adversarial training
    # ==================================================================================================================

    target_score_upper = torch.zeros(1).to(device)
    target_score_lower = torch.zeros(1).to(device)

    # calculate source weight
    source_class_weight = calc_source_class_weight(val_loader, classifier, ens_classifier, args)
    source_class_weight = (source_class_weight > args.cut).float()

    print('weight of source classes')
    print(source_class_weight.cpu())

    # start training
    print('Stage 2: Adversarial Training')
    best_acc = 0.
    best_h_score = 0.
    for epoch in range(args.epochs):
        # train for one epoch
        train(train_source_iter, train_target_iter, classifier, domain_adv, ens_classifier, optimizer, lr_scheduler,
              source_class_weight, target_score_upper, target_score_lower, epoch, args)

        for i in range(5):
            train_ens_classifier(ens_iters[i], classifier, ens_classifier, ens_optimizer, ens_lr_scheduler[i], i, epoch,
                                 args)

        # evaluate on validation set
        acc, h_score = validate(val_loader, classifier, ens_classifier, num_classes, num_common_classes, args)
        torch.save(classifier.state_dict(), logger.get_checkpoint_path('latest_classifier'))
        torch.save(ens_classifier.state_dict(), logger.get_checkpoint_path('latest_ens_classifier'))

        best_acc = max(acc, best_acc)
        if h_score > best_h_score:
            best_h_score = h_score
            # remember best h_score and save checkpoint
            shutil.copy(logger.get_checkpoint_path('latest_classifier'), logger.get_checkpoint_path('best_classifier'))
            shutil.copy(logger.get_checkpoint_path('latest_ens_classifier'),
                        logger.get_checkpoint_path('best_ens_classifier'))

    print('* Val Best Mean Acc@1 {:.3f}'.format(best_acc))
    print('* Val Best H-score {:.3f}'.format(best_h_score))

    # evaluate on test set
    classifier.load_state_dict(torch.load(logger.get_checkpoint_path('best_classifier')))
    ens_classifier.load_state_dict(torch.load(logger.get_checkpoint_path('best_ens_classifier')))
    test_acc, test_h_score = validate(test_loader, classifier, ens_classifier, num_classes, num_common_classes, args)
    print('* Test Mean Acc@1 {:.3f} H-score {:.3f}'.format(test_acc, test_h_score))
    logger.close()


def pretrain(train_source_iter: ForeverDataIterator, ens_iters, model: ImageClassifier, ens_classifier: Ensemble,
             optimizer_pretrain: SGD, lr_scheduler_pretrain: LambdaLR, epoch: int, args: argparse.Namespace):
    losses = AverageMeter('Loss', ':3.2f')
    cls_accs = AverageMeter('Cls Acc', ':3.1f')
    progress = ProgressMeter(
        args.iters_per_epoch,
        [losses, cls_accs],
        prefix="Pretrain Epoch: [{}]".format(epoch))

    model.train()
    ens_classifier.train()
    batch_size = args.batch_size

    for i in range(args.iters_per_epoch):
        x_s, labels_s = next(train_source_iter)
        x_s = x_s.to(device)
        labels_s = labels_s.to(device)

        # clear grad
        optimizer_pretrain.zero_grad()

        # compute output
        y_s, _ = model(x_s)
        cls_loss = F.cross_entropy(y_s, labels_s)
        cls_loss.backward()

        ens_loss = 0
        cls_acc = 0
        for classifier_idx, ens_iter in enumerate(ens_iters):
            x_s, labels_s = next(ens_iter)
            x_s = x_s.to(device)
            labels_s = labels_s.to(device)
            _, f_s = model(x_s)
            y_s = ens_classifier(f_s, index=classifier_idx)
            # cls loss of each classifier
            cls_loss = F.cross_entropy(y_s, labels_s)
            cls_loss.backward()

            ens_loss += cls_loss
            cls_acc += accuracy(y_s, labels_s)[0] / 5

        loss = ens_loss + cls_loss
        losses.update(loss.item(), batch_size)
        cls_accs.update(cls_acc.item(), batch_size)

        # compute gradient and do SGD step
        optimizer_pretrain.step()
        lr_scheduler_pretrain.step()

        if i % args.print_freq == 0:
            progress.display(i)


def calc_source_class_weight(val_loader: DataLoader, model: ImageClassifier, ens_classifier: Ensemble, args):
    # switch to evaluate mode
    model.eval()
    ens_classifier.eval()

    all_marginal_confidence = []
    all_entropy = []
    all_output = []

    with torch.no_grad():
        for images, labels in val_loader:
            images = images.to(device)
            output, f = model(images)
            output = F.softmax(output, -1)

            yt_1, yt_2, yt_3, yt_4, yt_5 = ens_classifier(f, -1)
            marginal_confidence = get_marginal_confidence(yt_1, yt_2, yt_3, yt_4, yt_5)
            entropy = get_entropy(yt_1, yt_2, yt_3, yt_4, yt_5)

            all_marginal_confidence.append(marginal_confidence)
            all_entropy.append(entropy)
            all_output.append(output)

    all_marginal_confidence = norm(torch.cat(all_marginal_confidence, dim=0))
    all_entropy = norm(torch.cat(all_entropy, dim=0))
    all_score = (all_marginal_confidence + 1 - all_entropy) / 2

    all_output = torch.cat(all_output, dim=0)
    source_class_weight = all_output[all_score >= args.src_threshold].mean(dim=0)
    source_class_weight = norm(source_class_weight)

    return source_class_weight


def train(train_source_iter: ForeverDataIterator, train_target_iter: ForeverDataIterator, model: ImageClassifier,
          domain_adv: DomainAdversarialLoss, ens_classifier: Ensemble, optimizer: SGD, lr_scheduler: LambdaLR,
          source_class_weight, target_score_upper, target_score_lower, epoch: int, args: argparse.Namespace):
    batch_time = AverageMeter('Time', ':3.2f')
    cls_losses = AverageMeter('Cls Loss', ':3.2f')
    transfer_losses = AverageMeter('Transfer Loss', ':3.2f')
    losses = AverageMeter('Loss', ':3.2f')
    cls_accs = AverageMeter('Cls Acc', ':3.1f')
    domain_accs = AverageMeter('Domain Acc', ':3.1f')
    score_upper = AverageMeter('Score Upper', ':3.2f')
    score_lower = AverageMeter('Score Lower', ':3.2f')
    progress = ProgressMeter(
        args.iters_per_epoch,
        [batch_time, cls_losses, transfer_losses, losses, cls_accs, domain_accs, score_upper, score_lower],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()
    domain_adv.train()
    ens_classifier.eval()

    batch_size = args.batch_size
    end = time.time()
    for i in range(args.iters_per_epoch):
        x_s, labels_s = next(train_source_iter)
        x_t, _ = next(train_target_iter)

        x_s = x_s.to(device)
        x_t = x_t.to(device)
        labels_s = labels_s.to(device)

        # compute output
        y_s, f_s = model(x_s)
        y_t, f_t = model(x_t)

        with torch.no_grad():
            yt_1, yt_2, yt_3, yt_4, yt_5 = ens_classifier(f_t, -1)
            marginal_confidence = get_marginal_confidence(yt_1, yt_2, yt_3, yt_4, yt_5)
            entropy = get_entropy(yt_1, yt_2, yt_3, yt_4, yt_5)

            # target weights
            w_t = (marginal_confidence + 1 - entropy) / 2
            target_score_upper = target_score_upper * 0.01 + w_t.max() * 0.99
            target_score_lower = target_score_lower * 0.01 + w_t.min() * 0.99
            w_t = (w_t - target_score_lower) / (target_score_upper - target_score_lower)

            # source weights
            w_s = source_class_weight[labels_s]

        cls_loss = F.cross_entropy(y_s, labels_s)
        transfer_loss = args.trade_off * domain_adv(f_s, f_t, w_s.detach(), w_t.detach())
        loss = cls_loss + transfer_loss

        cls_losses.update(cls_loss.item(), batch_size)
        transfer_losses.update(transfer_loss.item(), batch_size)
        losses.update(loss.item(), batch_size)

        cls_acc = accuracy(y_s, labels_s)[0]
        cls_accs.update(cls_acc.item(), batch_size)
        domain_acc = domain_adv.domain_discriminator_accuracy
        domain_accs.update(domain_acc.item(), batch_size)
        score_upper.update(target_score_upper.item(), batch_size)
        score_lower.update(target_score_lower.item(), batch_size)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        lr_scheduler.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)


def train_ens_classifier(train_source_iter: ForeverDataIterator, model: ImageClassifier, ens_classifier: Ensemble,
                         optimizer: SGD, lr_scheduler: LambdaLR, classifier_index: int, epoch: int,
                         args: argparse.Namespace):
    losses = AverageMeter('Loss', ':3.2f')
    cls_accs = AverageMeter('Cls Acc', ':3.1f')
    progress = ProgressMeter(
        args.iters_per_epoch // 2,
        [losses, cls_accs],
        prefix="Train ensemble classifier {}:, Epoch: [{}]".format(classifier_index + 1, epoch))

    model.eval()
    ens_classifier.train()

    batch_size = args.batch_size
    for i in range(args.iters_per_epoch // 2):
        x_s, labels_s = next(train_source_iter)
        x_s = x_s.to(device)
        labels_s = labels_s.to(device)

        # compute output
        with torch.no_grad():
            _, f_s = model(x_s)
        y_s = ens_classifier(f_s, classifier_index)

        loss = F.cross_entropy(y_s, labels_s)
        losses.update(loss.item(), batch_size)
        cls_acc = accuracy(y_s, labels_s)[0]
        cls_accs.update(cls_acc.item(), batch_size)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        lr_scheduler.step()

        if i % args.print_freq == 0:
            progress.display(i)


def validate(val_loader, model, ens_classifier, num_classes, num_common_classes, args):
    # switch to evaluate mode
    model.eval()
    ens_classifier.eval()

    all_marginal_confidence = []
    all_entropy = []
    all_predictions = []
    all_labels = []

    with torch.no_grad():
        for images, label in val_loader:
            images = images.to(device)
            label = label.to(device)

            output, f = model(images)
            _, prediction = torch.max(F.softmax(output, -1), 1)

            yt_1, yt_2, yt_3, yt_4, yt_5 = ens_classifier(f, -1)
            marginal_confidence = get_marginal_confidence(yt_1, yt_2, yt_3, yt_4, yt_5)
            entropy = get_entropy(yt_1, yt_2, yt_3, yt_4, yt_5)

            all_marginal_confidence.append(marginal_confidence)
            all_entropy.append(entropy)
            all_predictions.append(prediction)
            all_labels.append(label)

    all_marginal_confidence = norm(torch.cat(all_marginal_confidence, dim=0))
    all_entropy = norm(torch.cat(all_entropy, dim=0))
    all_score = (all_marginal_confidence + 1 - all_entropy) / 2

    all_predictions = torch.cat(all_predictions, dim=0)
    all_labels = torch.cat(all_labels, dim=0)

    unknown_class = num_classes
    confmat = ConfusionMatrix(num_classes=num_classes + 1)

    all_predictions[all_score < args.threshold] = unknown_class
    all_labels[all_labels >= unknown_class] = unknown_class
    confmat.update(all_labels, all_predictions)

    _, accs, _ = confmat.compute()
    mean_acc = ((accs[:num_common_classes].sum() + accs[-1]) / (num_common_classes + 1)).item() * 100
    known = accs[:num_common_classes].mean().item() * 100
    unknown = accs[-1].item() * 100
    h_score = 2 * known * unknown / (known + unknown)

    print('* Mean Acc@1 {:.3f}'.format(mean_acc))
    print('* Known Acc@1 {:.3f}'.format(known))
    print('* Unknown Acc@1 {:.3f}'.format(unknown))
    print('* H-score {:.3f}'.format(h_score))

    return mean_acc, h_score


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CMU for Universal Domain Adaptation')
    # dataset parameters
    parser.add_argument('root', metavar='DIR',
                        help='root path of dataset')
    parser.add_argument('-d', '--data', metavar='DATA', default='Office31', choices=utils.get_dataset_names(),
                        help='dataset: ' + ' | '.join(utils.get_dataset_names()) +
                             ' (default: Office31)')
    parser.add_argument('-s', '--source', help='source domain')
    parser.add_argument('-t', '--target', help='target domain')
    parser.add_argument('--train-resizing', type=str, default='default')
    parser.add_argument('--val-resizing', type=str, default='default')
    # model parameters
    parser.add_argument('-a', '--arch', metavar='ARCH', default='resnet50',
                        choices=utils.get_model_names(),
                        help='backbone architecture: ' +
                             ' | '.join(utils.get_model_names()) +
                             ' (default: resnet50)')
    parser.add_argument('--no-pool', action='store_true',
                        help='no pool layer after the feature extractor.')
    parser.add_argument('--bottleneck-dim', default=256, type=int,
                        help='Dimension of bottleneck')
    # training parameters
    parser.add_argument('--threshold', default=0.7, type=float,
                        help='When class confidence is less than the given threshold, '
                             'model will output "unknown" (default: 0.7)')
    parser.add_argument('--src-threshold', default=0.4, type=float,
                        help='threshold for source common class item counting (default: 0.4)')
    parser.add_argument('--cut', default=0.1, type=float,
                        help='cut threshold for common classes identifying (default: 0.1)')
    parser.add_argument('--trade-off', default=1., type=float,
                        help='the trade-off hyper-parameter for transfer loss')
    parser.add_argument('-b', '--batch-size', default=32, type=int,
                        metavar='N',
                        help='mini-batch size (default: 32)')
    parser.add_argument('--lr', '--learning-rate', default=0.001, type=float,
                        metavar='LR', help='initial learning rate', dest='lr')
    parser.add_argument('--lr-gamma', default=0.001, type=float, help='parameter for lr scheduler')
    parser.add_argument('--lr-decay', default=0.75, type=float, help='parameter for lr scheduler')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=1e-3, type=float,
                        metavar='W', help='weight decay (default: 1e-3)',
                        dest='weight_decay')
    parser.add_argument('-j', '--workers', default=2, type=int, metavar='N',
                        help='number of data loading workers (default: 2)')
    parser.add_argument('--epochs', default=30, type=int, metavar='N',
                        help='number of total epochs to run (default: 30)')
    parser.add_argument('--epochs-pretrain', default=5, type=int,
                        help='number of total epochs to run in the pretraining stage (default: 5)')
    parser.add_argument('-i', '--iters-per-epoch', default=200, type=int,
                        help='Number of iterations per epoch (default: 200)')
    parser.add_argument('-p', '--print-freq', default=50, type=int,
                        metavar='N', help='print frequency (default: 50)')
    parser.add_argument('--seed', default=None, type=int,
                        help='seed for initializing training. ')
    parser.add_argument("--log", type=str, default='cmu',
                        help="Where to save logs, checkpoints and debugging images.")
    parser.add_argument("--phase", type=str, default='train', choices=['train', 'test', 'analysis'],
                        help="When phase is 'test', only test the model."
                             "When phase is 'analysis', only analysis the model.")
    args = parser.parse_args()
    main(args)
