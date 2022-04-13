import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.ops.boxes import box_area


def tensor_cxcywh_to_xyxy(x):
    x_c, y_c, w, h = x.unbind(-1)
    b = [(x_c - 0.5 * w), (y_c - 0.5 * h),
         (x_c + 0.5 * w), (y_c + 0.5 * h)]
    return torch.stack(b, dim=-1)


def tensor_xyxy_to_cxcywh(x):
    x0, y0, x1, y1 = x.unbind(-1)
    b = [(x0 + x1) / 2, (y0 + y1) / 2,
         (x1 - x0), (y1 - y0)]
    return torch.stack(b, dim=-1)


def box_iou(boxes1, boxes2):
    area1 = box_area(boxes1)
    area2 = box_area(boxes2)

    lt = torch.max(boxes1[:, None, :2], boxes2[:, :2])  # [N,M,2]
    rb = torch.min(boxes1[:, None, 2:], boxes2[:, 2:])  # [N,M,2]

    wh = (rb - lt).clamp(min=0)  # [N,M,2]
    inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]

    union = area1[:, None] + area2 - inter

    iou = inter / union
    return iou, union

def generalized_box_iou(boxes1, boxes2):
    """
    Generalized IoU from https://giou.stanford.edu/

    The boxes should be in [x0, y0, x1, y1] format

    Returns a [N, M] pairwise matrix, where N = len(boxes1)
    and M = len(boxes2)
    """
    # degenerate boxes gives inf / nan results
    # so do an early check
    assert (boxes1[:, 2:] >= boxes1[:, :2]).all()
    assert (boxes2[:, 2:] >= boxes2[:, :2]).all()
    iou, union = box_iou(boxes1, boxes2)

    lt = torch.min(boxes1[:, None, :2], boxes2[:, :2])
    rb = torch.max(boxes1[:, None, 2:], boxes2[:, 2:])

    wh = (rb - lt).clamp(min=0)  # [N,M,2]
    area = wh[:, :, 0] * wh[:, :, 1]

    return iou - (area - union) / area


class MutiLoss(nn.Module):
    def __init__(self,):
        super(MutiLoss, self).__init__()
        self.l1_weight = 1
        self.giou_weight = 1
    
    def forward(self, boxes, boxes_trg, num_boxes, seq_mask):
        '''
        boxes&boxes_trg:(nb,seq_l_4)
        seq_mask:(nb,seq_l)
        '''
        seq_mask = seq_mask.reshape(-1).unsqueeze(-1).repeat(1,4)
        
        boxes = boxes.reshape(-1,4)
        boxes = torch.masked_select(boxes,seq_mask)
        boxes = boxes.reshape(-1,4)

        boxes_trg = boxes_trg.reshape(-1,4)
        boxes_trg = torch.masked_select(boxes_trg,seq_mask)
        boxes_trg = boxes_trg.reshape(-1,4)
        test = tensor_cxcywh_to_xyxy(boxes_trg)

        loss_l1 = F.l1_loss(boxes, boxes_trg, reduction='none')
        loss_l1 = loss_l1.sum() / num_boxes

        loss_giou = 1 - torch.diag(generalized_box_iou(tensor_cxcywh_to_xyxy(boxes),tensor_cxcywh_to_xyxy(boxes_trg)))
        loss_giou = loss_giou.sum() / num_boxes

        loss = self.l1_weight*loss_l1 + self.giou_weight*loss_giou
        return loss

