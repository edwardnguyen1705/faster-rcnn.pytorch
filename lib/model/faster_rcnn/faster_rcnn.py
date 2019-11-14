import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.models as models
from torch.autograd import Variable
import numpy as np
from model.utils.config import cfg
from model.rpn.rpn import _RPN

from model.roi_layers import ROIAlign, ROIPool

# from model.roi_pooling.modules.roi_pool import _RoIPooling
# from model.roi_align.modules.roi_align import RoIAlignAvg


from model.rpn.proposal_target_layer_cascade import _ProposalTargetLayer
import time
import pdb
from model.utils.net_utils import _smooth_l1_loss, _crop_pool_layer, _affine_grid_gen, _affine_theta

class _fasterRCNN(nn.Module):
    """ faster RCNN """
    def __init__(self, classes, class_agnostic, poses):
        super(_fasterRCNN, self).__init__()
        self.classes = classes
        self.n_classes = len(classes)
        self.n_poses = len(poses)
        self.class_agnostic = class_agnostic
        # loss
        self.RCNN_loss_cls = 0
        self.RCNN_loss_bbox = 0
        
        self.RCNN_loss_ps = 0

        # define rpn
        self.RCNN_rpn = _RPN(self.dout_base_model)  # self.dout_base_model = 512
        self.RCNN_proposal_target = _ProposalTargetLayer(self.n_classes)

        # self.RCNN_roi_pool = _RoIPooling(cfg.POOLING_SIZE, cfg.POOLING_SIZE, 1.0/16.0)
        # self.RCNN_roi_align = RoIAlignAvg(cfg.POOLING_SIZE, cfg.POOLING_SIZE, 1.0/16.0)

        self.RCNN_roi_pool = ROIPool((cfg.POOLING_SIZE, cfg.POOLING_SIZE), 1.0/16.0)
        self.RCNN_roi_align = ROIAlign((cfg.POOLING_SIZE, cfg.POOLING_SIZE), 1.0/16.0, 0)

    def forward(self, im_data, im_info, gt_boxes, num_boxes):
        batch_size = im_data.size(0)
        #print('FastRCNN gt_boxes: {}'.format(gt_boxes.shape))
        
        im_info = im_info.data
        #gt_boxes = gt_boxes.data
        
        gt_boxes_org = gt_boxes    
        gt_boxes = gt_boxes.data[:,:,0:5]
        #print('gt_classes: {}'.format(gt_boxes[:,:,-1]))
        
        #print('gt_boxes_org.data: {}'.format(gt_boxes_org.data.shape))
        gt_poses = gt_boxes_org.data[:,:,-1]
        #print('gt_poses: {}'.format(gt_poses))
        
        num_boxes = num_boxes.data

        # feed image data to base model (feature extractor) to obtain base feature map
        # ex 13 conv layers of VGG16
        base_feat = self.RCNN_base(im_data)

        # feed base feature map tp RPN to obtain rois
        rois, rpn_loss_cls, rpn_loss_bbox = self.RCNN_rpn(base_feat, im_info, gt_boxes, num_boxes)

        # if it is training phrase, then use ground truth bboxes for refining
        if self.training:
            print('rois.shape: {0}'.format(rois.shape))
            roi_data = self.RCNN_proposal_target(rois, gt_boxes, num_boxes, gt_poses)
            rois, rois_label, rois_target, rois_inside_ws, rois_outside_ws, rois_pose = roi_data

            rois_label = Variable(rois_label.view(-1).long())
            rois_target = Variable(rois_target.view(-1, rois_target.size(2)))
            rois_inside_ws = Variable(rois_inside_ws.view(-1, rois_inside_ws.size(2)))
            rois_outside_ws = Variable(rois_outside_ws.view(-1, rois_outside_ws.size(2)))
            
            rois_pose = Variable(rois_pose.view(-1).long())
            
        else:
            rois_label = None
            rois_target = None
            rois_inside_ws = None
            rois_outside_ws = None
            rpn_loss_cls = 0
            rpn_loss_bbox = 0
            
            rois_pose = None

        rois = Variable(rois)
        # do roi pooling based on predicted rois

        if cfg.POOLING_MODE == 'align':
            pooled_feat = self.RCNN_roi_align(base_feat, rois.view(-1, 5))
        elif cfg.POOLING_MODE == 'pool':
            pooled_feat = self.RCNN_roi_pool(base_feat, rois.view(-1,5))

        ''' output from the shared fc7 of fast rcnn. pooled_feat will be fed to two fc layers:
            fc8_1: RCNN_bbox_pred
            fc8_2: RCNN_cls_score
        '''
        pooled_feat_org = pooled_feat
        #print('pooled_feat: {}'.format(pooled_feat.shape))
        pooled_feat = self._head_to_tail(pooled_feat)
        #print('pooled_feat: {}'.format(pooled_feat.shape))
        
        pooled_feat_pose = self._head_to_tail_pose(pooled_feat_org) 
        #print('pooled_feat_pose: {}'.format(pooled_feat_pose.shape))

        # compute bbox offset
        bbox_pred = self.RCNN_bbox_pred(pooled_feat)
        #print('bbox_pred: {}'.format(bbox_pred.shape))
        if self.training and not self.class_agnostic:
            # select the corresponding columns according to roi labels
            bbox_pred_view = bbox_pred.view(bbox_pred.size(0), int(bbox_pred.size(1) / 4), 4)
            bbox_pred_select = torch.gather(bbox_pred_view, 1, rois_label.view(rois_label.size(0), 1, 1).expand(rois_label.size(0), 1, 4))
            bbox_pred = bbox_pred_select.squeeze(1)

        
        #print('bbox_pred: {}'.format(bbox_pred.shape))
        # compute object classification probability
        cls_score = self.RCNN_cls_score(pooled_feat)
        cls_prob = F.softmax(cls_score, 1)
        
        #ps_score = self.RCNN_ps_score(pooled_feat)
        ps_score = self.RCNN_ps_score(pooled_feat_pose)
        ps_prob = F.softmax(ps_score, 1)

        RCNN_loss_cls = 0
        RCNN_loss_bbox = 0
        
        RCNN_loss_ps = 0

        if self.training:
            # classification loss
            RCNN_loss_cls = F.cross_entropy(cls_score, rois_label)
            print('rois_label: {}'.format(rois_label))

            # bounding box regression L1 loss
            RCNN_loss_bbox = _smooth_l1_loss(bbox_pred, rois_target, rois_inside_ws, rois_outside_ws)

            RCNN_loss_ps = F.cross_entropy(ps_score, rois_pose)
            print('rois_pose: {}'.format(rois_pose))
            
        cls_prob = cls_prob.view(batch_size, rois.size(1), -1)
        bbox_pred = bbox_pred.view(batch_size, rois.size(1), -1)

        return rois, cls_prob, bbox_pred, rpn_loss_cls, rpn_loss_bbox, RCNN_loss_cls, RCNN_loss_bbox, rois_label, ps_prob, RCNN_loss_ps, rois_pose

    def _init_weights(self):
        def normal_init(m, mean, stddev, truncated=False):
            """
            weight initalizer: truncated normal and random normal.
            """
            # x is a parameter
            if truncated:
                m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean) # not a perfect approximation
            else:
                m.weight.data.normal_(mean, stddev)
                m.bias.data.zero_()

        normal_init(self.RCNN_rpn.RPN_Conv, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_rpn.RPN_cls_score, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_rpn.RPN_bbox_pred, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_cls_score, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_bbox_pred, 0, 0.001, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_ps_score, 0, 0.01, cfg.TRAIN.TRUNCATED)

    def create_architecture(self):
        self._init_modules()    # 
        self._init_weights()    # rpn, 2-specific-task fast rcnn fc layers
        
