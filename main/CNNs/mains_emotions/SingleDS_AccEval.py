# Copyright (c) 2018 Zijun Wei.
# Licensed under the MIT License.
# Author: Zijun Wei
# Usage(TODO): modified from https://github.com/pytorch/examples/blob/master/imagenet/main.py
# TODO: compared to v1, this version adjusts the input parameters;
# TODO: compared to v2, this requires the scripts to be defined with separate file loader specifications
# TODO: updated as Main_Sclass_ACCEval: preservse previous good parts, now specified in single class, acc based evaluations
# Email: hzwzijun@gmail.com
# Created: 25/Jun/2019 11:09
import os, sys
project_root = os.path.join(os.path.expanduser('~'), 'Projects/MetaEmotion')
sys.path.append(project_root)

import argparse
import random
import shutil
import time
import warnings

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import CNNs.models as models
import CNNs.utils.util as CNN_utils
from torch.optim import lr_scheduler
from CNNs.dataloaders.utils import none_collate
from PyUtils.file_utils import get_date_str, get_dir, get_stem
from PyUtils import log_utils
import CNNs.dataset_loaders as custom_datasets
from CNNs.utils.config import parse_config
import torch.nn.functional as F
from CNNs.losses.z_loss import MclassCrossEntropyLoss
from CNNs.models.resnet import load_state_dict

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))


def get_instance(module, name, args):
    return getattr(module, name)(args)


def main():

    import argparse
    parser = argparse.ArgumentParser(description="Pytorch Image CNN training from Configure Files")
    parser.add_argument('--config_file', required=True, help="This scripts only accepts parameters from Json files")
    input_args = parser.parse_args()

    config_file = input_args.config_file

    args = parse_config(config_file)
    if args.name is None:
        args.name = get_stem(config_file)

    torch.set_default_tensor_type('torch.FloatTensor')
    best_prec1 = 0

    args.script_name = get_stem(__file__)
    current_time_str = get_date_str()
    if args.resume is None:
        if args.save_directory is None:
            save_directory = get_dir(os.path.join(project_root, 'ckpts', '{:s}'.format(args.name), '{:s}-{:s}'.format(args.ID, current_time_str)))
        else:
            save_directory = get_dir(os.path.join(project_root, 'ckpts', args.save_directory))
    else:
        save_directory = os.path.dirname(args.resume)
    print("Save to {}".format(save_directory))
    log_file = os.path.join(save_directory, 'log-{0}.txt'.format(current_time_str))
    logger = log_utils.get_logger(log_file)
    log_utils.print_config(vars(args), logger)


    print_func = logger.info
    print_func('ConfigFile: {}'.format(config_file))
    args.log_file = log_file

    if args.device:
        os.environ["CUDA_VISIBLE_DEVICES"]=args.device


    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    args.distributed = args.world_size > 1

    if args.distributed:
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size)

    if args.pretrained:
        print_func("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True, num_classes=args.num_classes)
    else:
        print_func("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=False, num_classes=args.num_classes)

    if args.freeze:
        model = CNN_utils.freeze_all_except_fc(model)

    if args.gpu is not None:
        model = model.cuda(args.gpu)
    elif args.distributed:
        model.cuda()
        model = torch.nn.parallel.DistributedDataParallel(model)
    else:
        if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
            model.features = torch.nn.DataParallel(model.features)
            model.cuda()
        else:
            model = torch.nn.DataParallel(model).cuda()

    # # define loss function (criterion) and optimizer
    # # # Update: here
    # # config = {'loss': {'type': 'simpleCrossEntropyLoss', 'args': {'param': None}}}
    # # criterion = get_instance(loss_funcs, 'loss', config)
    # # criterion = criterion.cuda(args.gpu)
    #
    criterion = nn.CrossEntropyLoss(ignore_index=-1).cuda(args.gpu)
    # criterion = MclassCrossEntropyLoss().cuda(args.gpu)


    optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    if args.lr_schedule:
        print_func("Using scheduled learning rate")
        scheduler = lr_scheduler.MultiStepLR(
            optimizer, [int(i) for i in args.lr_schedule.split(',')], gamma=0.1)
    else:
        scheduler = lr_scheduler.ReduceLROnPlateau(
            optimizer, 'min', patience=args.lr_patience)



    # optionally resume from a checkpoint
    # if args.resume:
    #     if os.path.isfile(args.resume):
    #         print_func("=> loading checkpoint '{}'".format(args.resume))
    #         checkpoint = torch.load(args.resume)
    #         args.start_epoch = checkpoint['epoch']
    #         best_prec1 = checkpoint['best_prec1']
    #         model.load_state_dict(checkpoint['state_dict'])
    #         optimizer.load_state_dict(checkpoint['optimizer'])
    #         print_func("=> loaded checkpoint '{}' (epoch {})"
    #               .format(args.resume, checkpoint['epoch']))
    #     else:
    #         print_func("=> no checkpoint found at '{}'".format(args.resume))

    if args.resume:
        if os.path.isfile(args.resume):
            print_func("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            import collections
            if not args.evaluate:
                if isinstance(checkpoint, collections.OrderedDict):
                    load_state_dict(model, checkpoint, exclude_layers=['fc.weight', 'fc.bias'])


                else:
                    load_state_dict(model, checkpoint['state_dict'], exclude_layers=['module.fc.weight', 'module.fc.bias'])
                    print_func("=> loaded checkpoint '{}' (epoch {})"
                          .format(args.resume, checkpoint['epoch']))
            else:
                if isinstance(checkpoint, collections.OrderedDict):
                    load_state_dict(model, checkpoint,strict=True)


                else:
                    load_state_dict(model, checkpoint['state_dict'],strict=True)
                    print_func("=> loaded checkpoint '{}' (epoch {})"
                               .format(args.resume, checkpoint['epoch']))
        else:
            print_func("=> no checkpoint found at '{}'".format(args.resume))
            return



    cudnn.benchmark = True

    model_total_params = sum(p.numel() for p in model.parameters())
    model_grad_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print_func("Total Parameters: {0}\t Gradient Parameters: {1}".format(model_total_params, model_grad_params))

    # Data loading code
    val_dataset = get_instance(custom_datasets, args.val_loader, args)
    if val_dataset is None:
        val_loader = None
    else:
        val_loader = torch.utils.data.DataLoader(val_dataset,
                                             batch_size=args.batch_size, shuffle=False,
                                             num_workers=args.workers, pin_memory=True, collate_fn=none_collate)

    if args.evaluate:
        validate(val_loader, model, criterion, args, print_func)
        return
    else:

        train_dataset = get_instance(custom_datasets, args.train_loader, args)

        if args.distributed:
            train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        else:
            train_sampler = None

        train_loader = torch.utils.data.DataLoader(
            train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
            num_workers=args.workers, pin_memory=True, sampler=train_sampler, collate_fn=none_collate)




    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        if args.lr_schedule:
            # CNN_utils.adjust_learning_rate(optimizer, epoch, args.lr)
            scheduler.step()
        current_lr = optimizer.param_groups[0]['lr']

        print_func("Epoch: [{}], learning rate: {}".format(epoch, current_lr))

        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, args, print_func)

        # evaluate on validation set
        if val_loader:
            prec1, val_loss = validate(val_loader, model, criterion, args, print_func)
        else:
            prec1 = 0
            val_loss = 0
        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        CNN_utils.save_checkpoint({
            'epoch': epoch,
            'arch': args.arch,
            'state_dict': model.module.state_dict() if hasattr(model, "module") else model.state_dict(),
            'best_prec1': best_prec1.item(),
            'optimizer' : optimizer.state_dict(),
        }, is_best, file_directory=save_directory, epoch=epoch, save_best_only=args.save_best_only)

        if not args.lr_schedule:
            scheduler.step(val_loss)


def train(train_loader, model, criterion, optimizer, epoch, args, print_func):
    batch_time = CNN_utils.AverageMeter()
    data_time = CNN_utils.AverageMeter()
    losses = CNN_utils.AverageMeter()
    top1 = CNN_utils.AverageMeter()
    topX = CNN_utils.AverageMeter()

    kout = args.topX or args.num_classes // 2

    # switch to train mode
    model.train()
    if args.fix_BN:
        CNN_utils.fix_BN(model)
    end = time.time()

    for i, (input, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        if args.gpu is not None:
            input = input.cuda(args.gpu, non_blocking=True)
        target = target.cuda(args.gpu, non_blocking=True)
        # target_idx = target.nonzero() [:,1]


        # compute output
        output = model(input)
        loss = criterion(output, target)

        losses.update(loss.item(), input.size(0))

        prec1, precX = CNN_utils.accuracy(output, target, topk=(1, kout))

        top1.update(prec1[0], input.size(0))
        topX.update(precX[0], input.size(0))

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            print_func('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@{kout} {topX.val:.3f} ({topX.avg:.3f})'.format(
                   epoch, i, len(train_loader), batch_time=batch_time,
                   data_time=data_time, loss=losses, top1=top1, kout=kout, topX=topX))


def validate(val_loader, model, criterion, args, print_func):
    if val_loader is None:
        return  0, 0
    batch_time = CNN_utils.AverageMeter()
    losses = CNN_utils.AverageMeter()
    top1 = CNN_utils.AverageMeter()
    topX = CNN_utils.AverageMeter()

    kout = args.topX or args.num_classes // 2

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        end = time.time()
        for i, (input, target) in enumerate(val_loader):
            if args.gpu is not None:
                input = input.cuda(args.gpu, non_blocking=True)

            target = target.cuda(args.gpu, non_blocking=True)


            # compute output

            output = model(input)

            loss = criterion(output, target)

            losses.update(loss.item(), input.size(0))

            prec1, precX = CNN_utils.accuracy(output, target, topk=(1, kout))

            top1.update(prec1[0], input.size(0))
            topX.update(precX[0], input.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                print_func('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                      'Prec@{kout} {topX.val:.3f} ({topX.avg:.3f})'.format(
                       i, len(val_loader), batch_time=batch_time, loss=losses,
                       top1=top1, kout=kout, topX = topX))

        print_func(' * Prec@1 {top1.avg:.3f}'
              .format(top1=top1))

    return top1.avg, losses.avg


if __name__ == '__main__':
    main()

