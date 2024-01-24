
from __future__ import print_function
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import time
import warnings
import os, sys
from apex import amp
warnings.filterwarnings('ignore')

## Input interpolation functions
def mix_data_lab(x, y, alpha=1.0, device='cuda'):
    '''Returns mixed inputs, pairs of targets, and lambda'''
    if alpha > 0:
        lam = np.random.beta(alpha, alpha)
    else:
        lam = 1

    batch_size = x.size()[0]
    index = torch.randperm(batch_size).to(device)

    lam = max(lam, 1 - lam)
    mixed_x = lam * x + (1 - lam) * x[index, :]
    y_a, y_b = y, y[index]

    return mixed_x, y_a, y_b, index, lam

def unsupervised_masks_estimation(args, queue, mix_index1, mix_index2, epoch, bsz, device):
    labelsUnsup = torch.arange(bsz).long().unsqueeze(1).to(device)  # If no labels used, label is the index in mini-batch
    maskUnsup_batch = torch.eye(bsz, dtype=torch.float32).to(device)
    maskUnsup_batch = maskUnsup_batch.repeat(2, 2)
    maskUnsup_batch[torch.eye(2 * bsz) == 1] = 0  ##remove self-contrast case

    if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
        ## Extend mask to consider queue features (all zeros except for the last features stored that contain the augmented view in the memory
        maskUnsup_mem = torch.zeros((2 * bsz, queue.K)).float().to(device)  ##Mini-batch samples with memory samples (add columns)

        ##Re-use measkUnsup_batch to copy it in the memory (in the righ place) and find the augmented views (without gradients)

        if queue.ptr == 0:
            maskUnsup_mem[:, -2 * bsz:] = maskUnsup_batch
        else:
            maskUnsup_mem[:, queue.ptr - (2 * bsz):queue.ptr] = maskUnsup_batch

    else:
        maskUnsup_mem = []

    ######################### Mixup additional mask: unsupervised term ######################
    ## With no labels (labelUnsup is just the index in the mini-batch, i.e. different for each sample)
    quad1_unsup = torch.eq(labelsUnsup[mix_index1], labelsUnsup.t()).float()  ##Minor label in 1st mini-batch part equal to mayor label in the first mini-batch part (note that mayor label of 1st and 2nd is the same as we force the original image to always be the dominant)
    quad2_unsup = quad1_unsup
    ##Minor label in 1st mini-batch part equal to mayor label in the second mini-batch part
    quad3_unsup = torch.eq(labelsUnsup[mix_index2], labelsUnsup.t()).float()  ##Minor label in 2nd mini-batch part equal to mayor label in the first mini-batch part
    quad4_unsup = quad3_unsup
    ##Minor label in 2nd mini-batch part equal to mayor label in the second mini-batch part

    mask2_a_unsup = torch.cat((quad1_unsup, quad2_unsup), dim=1)
    mask2_b_unsup = torch.cat((quad3_unsup, quad4_unsup), dim=1)
    mask2Unsup_batch = torch.cat((mask2_a_unsup, mask2_b_unsup), dim=0)

    ## Make sure diagonal is zero (i.e. not taking as positive my own sample)
    mask2Unsup_batch[torch.eye(2 * bsz) == 1] = 0 

    if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
        ## Extend mask to consider queue features (will be zeros excpet the positions for the augmented views for the second mixup term)
        mask2Unsup_mem = torch.zeros((2 * bsz, queue.K)).float().to(device)  ##Mini-batch samples with memory samples (add columns)

        if queue.ptr == 0:
            mask2Unsup_mem[:, -2 * bsz:] = mask2Unsup_batch
        else:
            mask2Unsup_mem[:, queue.ptr - (2 * bsz):queue.ptr] = mask2Unsup_batch

    else:
        mask2Unsup_mem = []


    return maskUnsup_batch, maskUnsup_mem, mask2Unsup_batch, mask2Unsup_mem

                        
def supervised_masks_estimation2(sample_weight, confident_sample, args, index, queue, queue_index, mix_index1, mix_index2, epoch, bsz, device):
    ###################### Supervised mask excluding augmented view ###############################
    #labels = labels.contiguous().view(-1, 1)
    queue_index = queue_index.cuda().long()

    if index.shape[0] != bsz:
        raise ValueError('Num of labels does not match num of features')
   
    noisy_pairs=torch.eq(confident_sample[index].unsqueeze(1), confident_sample[index].unsqueeze(1).t()).cuda()
    temp_graph = torch.zeros(len(index),len(index)).type(torch.uint8).cuda().float()
    
    selected_index1=(confident_sample[index] >= 0).nonzero().reshape(-1)
    selected_index2=(confident_sample[index] >= 0).nonzero().reshape(-1)

    temp_graph[selected_index1.unsqueeze(1).expand(len(selected_index1),len(selected_index2)),selected_index2.unsqueeze(0).expand(len(selected_index1),len(selected_index2))] \
    = noisy_pairs[selected_index1.unsqueeze(1).expand(len(selected_index1),len(selected_index2)),selected_index2.unsqueeze(0).expand(len(selected_index1),len(selected_index2))].float()
    
    #weighting
    pairs_pro = sample_weight[index].expand(bsz,bsz) * sample_weight[index].expand(bsz,bsz).t()
    temp_graph_pairs = temp_graph.clone()
    temp_graph = temp_graph * pairs_pro
    


    ##Create mask without diagonal to avoid augmented view, i.e. this is supervised mask
    maskSup_batch = temp_graph.float().to(device) 
    maskSup_batch [torch.eye(bsz) == 1] = 0
    #- torch.eye(bsz, dtype=torch.float32).to(device)
    #torch.eq(labels, labels.t()).float() - torch.eye(bsz, dtype=torch.float32).to(device)
    maskSup_batch = maskSup_batch.repeat(2, 2)
    maskSup_batch[torch.eye(2 * bsz) == 1] = 0  ##remove self-contrast case

    if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
        ## Extend mask to consider queue features
          
        noisy_pairs=torch.eq(confident_sample[index].unsqueeze(1), confident_sample[queue_index].unsqueeze(1).t())
        temp_graph_mem = torch.zeros(len(index),len(queue_index)).type(torch.uint8).cuda().float()
        
        selected_index1=(confident_sample[index] >=0).nonzero().reshape(-1)
        selected_index2=(confident_sample[queue_index] >=0).nonzero().reshape(-1)
        temp_graph_mem[selected_index1.unsqueeze(1).expand(len(selected_index1),len(selected_index2)),selected_index2.unsqueeze(0).expand(len(selected_index1),len(selected_index2))] \
        = noisy_pairs[selected_index1.unsqueeze(1).expand(len(selected_index1),len(selected_index2)),selected_index2.unsqueeze(0).expand(len(selected_index1),len(selected_index2))].float()
        
        #weighting
        pair_pro_mem = sample_weight[index].unsqueeze(0).t().expand(bsz,len(queue_index)) * sample_weight[queue_index].expand(bsz,len(queue_index))
        temp_graph_mem = temp_graph_mem * pair_pro_mem


        maskSup_mem = temp_graph_mem.float().repeat(2, 1).to(device)
        ##Mini-batch samples with memory samples (add columns)

        if queue.ptr == 0:
            maskSup_mem[:, -2 * bsz:] = maskSup_batch
        else:
            maskSup_mem[:, queue.ptr - (2 * bsz):queue.ptr] = maskSup_batch

    else:
        maskSup_mem = []

    ######################### Mixup additional mask: supervised term ######################
    ## With labels
    quad1_sup = temp_graph[mix_index1].float().to(device)
    ##Minor label in 1st mini-batch part equal to mayor label in the first mini-batch part (note that mayor label of 1st and 2nd is the same as we force the original image to always be the mayor/dominant)
    quad2_sup = quad1_sup
    ##Minor label in 1st mini-batch part equal to mayor label in the second mini-batch part
    quad3_sup = temp_graph[mix_index2].float().to(device)
    ##Minor label in 2nd mini-batch part equal to mayor label in the first mini-batch part
    quad4_sup = quad3_sup
    ##Minor label in 2nd mini-batch part equal to mayor label in the second mini-batch part

    mask2_a_sup = torch.cat((quad1_sup, quad2_sup), dim=1)
    mask2_b_sup = torch.cat((quad3_sup, quad4_sup), dim=1)
    mask2Sup_batch = torch.cat((mask2_a_sup, mask2_b_sup), dim=0)

    ## Make sure diagonal is zero (i.e. not taking as positive my own sample)
    mask2Sup_batch[torch.eye(2 * bsz) == 1] = 0

    if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
        ## Extend mask to consider queue features. Here we consider that the label for images is the minor one, i.e. labels[mix_index1], labels[mix_index2] and queue_labels_mix
        ## Here we don't repeat the columns part as in maskSup because the minor label is different for the first and second part of the mini-batch (different mixup shuffling for each mini-batch part)
        maskExtended_sup3_1 = temp_graph_mem[mix_index1].float().to(device)
        ##Mini-batch samples with memory samples (add columns)
        maskExtended_sup3_2 = temp_graph_mem[mix_index2].float().to(device)
        ##Mini-batch samples with memory samples (add columns)
        mask2Sup_mem = torch.cat((maskExtended_sup3_1, maskExtended_sup3_2), dim=0)

        if queue.ptr == 0:
            mask2Sup_mem[:, -2 * bsz:] = mask2Sup_batch

        else:
            mask2Sup_mem[:, queue.ptr - (2 * bsz):queue.ptr] = mask2Sup_batch

    else:
        mask2Sup_mem = []

    return maskSup_batch, maskSup_mem, mask2Sup_batch, mask2Sup_mem, temp_graph_pairs


def supervised_masks_estimation(ssl_select,sample_weight,selected_examples,args, index, queue, queue_index, mix_index1, mix_index2, epoch, bsz, device,confident_sample):
    ###################### Supervised mask excluding augmented view ###############################
    #labels = labels.contiguous().view(-1, 1)
    queue_index = queue_index.long().cuda()
   
    no_choose = (ssl_select[index] < 0).nonzero()[:, 0]
    pair_sel = torch.eq(ssl_select[index],ssl_select[index].unsqueeze(0).t()).long()
    pair_pro = sample_weight[index].expand(bsz,bsz) * sample_weight[index].expand(bsz,bsz).t()
    pair_sel[no_choose, :] = 0
    pair_sel[:, no_choose] = 0
    pair_sel[torch.eye(bsz) == 1] = 0
    sel_pro = pair_sel * pair_pro


    if index.shape[0] != bsz:
        raise ValueError('Num of labels does not match num of features')
   
    noisy_pairs=torch.eq(confident_sample[index].unsqueeze(1), confident_sample[index].unsqueeze(1).t()).cuda()
    temp_graph = torch.zeros(len(index),len(index)).type(torch.uint8).cuda().float()
   
    selected_index1=(selected_examples[index] >= 0).nonzero().reshape(-1)
    selected_index2=(selected_examples[index] >= 0).nonzero().reshape(-1)
    temp_graph[selected_index1.unsqueeze(1).expand(len(selected_index1),len(selected_index2)),selected_index2.unsqueeze(0).expand(len(selected_index1),len(selected_index2))] \
    = noisy_pairs[selected_index1.unsqueeze(1).expand(len(selected_index1),len(selected_index2)),selected_index2.unsqueeze(0).expand(len(selected_index1),len(selected_index2))].float()
    
    #weighting
    lable_sel_pro = sample_weight[index].expand(bsz,bsz) * sample_weight[index].expand(bsz,bsz).t()
    temp_graph = temp_graph * lable_sel_pro
    
    temp_graph =  temp_graph + sel_pro

    temp_graph[temp_graph > 1] = 1


    ##Create mask without diagonal to avoid augmented view, i.e. this is supervised mask
    maskSup_batch = temp_graph.float().to(device) 
    maskSup_batch [torch.eye(bsz) == 1] = 0
    #- torch.eye(bsz, dtype=torch.float32).to(device)
    #torch.eq(labels, labels.t()).float() - torch.eye(bsz, dtype=torch.float32).to(device)
    maskSup_batch = maskSup_batch.repeat(2, 2)
    maskSup_batch[torch.eye(2 * bsz) == 1] = 0  ##remove self-contrast case

    if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
        ## Extend mask to consider queue features
        
        no_choose = (ssl_select[index] < 0).nonzero()[:, 0]
        no_choose_queue = (ssl_select[queue_index] < 0).nonzero()[:, 0]
        pair_sel_mem = torch.eq(ssl_select[index].unsqueeze(0).t(),ssl_select[queue_index]).long()
        pair_pro_mem = sample_weight[index].unsqueeze(0).t().expand(bsz,len(queue_index)) * sample_weight[queue_index].expand(bsz,len(queue_index))
        pair_sel_mem[no_choose, :] = 0
        pair_sel_mem[:, no_choose_queue] = 0
        sel_pro_mem = pair_sel_mem * pair_pro_mem
          
        noisy_pairs=torch.eq(confident_sample[index].unsqueeze(1), confident_sample[queue_index].unsqueeze(1).t())
        temp_graph_mem = torch.zeros(len(index),len(queue_index)).type(torch.uint8).cuda().float()
        
        selected_index1=(selected_examples[index] >= 0).nonzero().reshape(-1)
        selected_index2=(selected_examples[queue_index] >= 0).nonzero().reshape(-1)
        temp_graph_mem[selected_index1.unsqueeze(1).expand(len(selected_index1),len(selected_index2)),selected_index2.unsqueeze(0).expand(len(selected_index1),len(selected_index2))] \
        = noisy_pairs[selected_index1.unsqueeze(1).expand(len(selected_index1),len(selected_index2)),selected_index2.unsqueeze(0).expand(len(selected_index1),len(selected_index2))].float()
        
        #weighting
        pair_pro_mem = sample_weight[index].unsqueeze(0).t().expand(bsz,len(queue_index)) * sample_weight[queue_index].expand(bsz,len(queue_index))
        temp_graph_mem = temp_graph_mem * pair_pro_mem

        temp_graph_mem = temp_graph_mem + sel_pro_mem
        

        maskSup_mem = temp_graph_mem.float().repeat(2, 1).to(device)
        ##Mini-batch samples with memory samples (add columns)

        if queue.ptr == 0:
            maskSup_mem[:, -2 * bsz:] = maskSup_batch
        else:
            maskSup_mem[:, queue.ptr - (2 * bsz):queue.ptr] = maskSup_batch

    else:
        maskSup_mem = []

    ######################### Mixup additional mask: supervised term ######################
    ## With labels
    quad1_sup = temp_graph[mix_index1].float().to(device)
    ##Minor label in 1st mini-batch part equal to mayor label in the first mini-batch part (note that mayor label of 1st and 2nd is the same as we force the original image to always be the mayor/dominant)
    quad2_sup = quad1_sup
    ##Minor label in 1st mini-batch part equal to mayor label in the second mini-batch part
    quad3_sup = temp_graph[mix_index2].float().to(device)
    ##Minor label in 2nd mini-batch part equal to mayor label in the first mini-batch part
    quad4_sup = quad3_sup
    ##Minor label in 2nd mini-batch part equal to mayor label in the second mini-batch part

    mask2_a_sup = torch.cat((quad1_sup, quad2_sup), dim=1)
    mask2_b_sup = torch.cat((quad3_sup, quad4_sup), dim=1)
    mask2Sup_batch = torch.cat((mask2_a_sup, mask2_b_sup), dim=0)

    ## Make sure diagonal is zero (i.e. not taking as positive my own sample)
    mask2Sup_batch[torch.eye(2 * bsz) == 1] = 0

    if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
        ## Extend mask to consider queue features. Here we consider that the label for images is the minor one, i.e. labels[mix_index1], labels[mix_index2] and queue_labels_mix
        ## Here we don't repeat the columns part as in maskSup because the minor label is different for the first and second part of the mini-batch (different mixup shuffling for each mini-batch part)
        maskExtended_sup3_1 = temp_graph_mem[mix_index1].float().to(device)
        ##Mini-batch samples with memory samples (add columns)
        maskExtended_sup3_2 = temp_graph_mem[mix_index2].float().to(device)
        ##Mini-batch samples with memory samples (add columns)
        mask2Sup_mem = torch.cat((maskExtended_sup3_1, maskExtended_sup3_2), dim=0)

        if queue.ptr == 0:
            mask2Sup_mem[:, -2 * bsz:] = mask2Sup_batch

        else:
            mask2Sup_mem[:, queue.ptr - (2 * bsz):queue.ptr] = mask2Sup_batch

    else:
        mask2Sup_mem = []

    return maskSup_batch, maskSup_mem, mask2Sup_batch, mask2Sup_mem