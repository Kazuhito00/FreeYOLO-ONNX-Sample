#!/usr/bin/env python
# -*- coding: utf-8 -*-
import copy

import cv2
import numpy as np
import onnxruntime


class FreeYOLO(object):

    def __init__(
        self,
        model_path,
        providers=[
            'CUDAExecutionProvider',
            'CPUExecutionProvider',
        ],
    ):
        # モデル読み込み
        self.onnx_session = onnxruntime.InferenceSession(
            model_path,
            providers=providers,
        )

        self.input_detail = self.onnx_session.get_inputs()[0]
        self.input_name = self.input_detail.name

        # 各種設定
        self.input_shape = self.input_detail.shape[2:]

    def __call__(self, image, num_classes=80, score_th=0.05, nms_th=0.5):
        temp_image = copy.deepcopy(image)

        # 前処理
        x, ratio = self._preprocess(
            temp_image,
            input_size=self.input_shape,
            swap=(2, 0, 1),
        )

        # 推論実施
        results = self.onnx_session.run(
            None,
            {self.input_name: x[None, :, :, :]},
        )

        # 後処理
        bboxes, scores, class_ids = self._postprocess(
            results[0],
            input_size=self.input_shape,
            strides=[8, 16, 32],
            num_classes=num_classes,
            conf_thresh=score_th,
            nms_thresh=nms_th,
        )
        if len(bboxes) > 0:
            bboxes /= ratio

        return bboxes, scores, class_ids

    def _preprocess(self, image, input_size=[640, 640], swap=(2, 0, 1)):
        if len(image.shape) == 3:
            padded_img = np.ones(
                (input_size[0], input_size[1], 3), np.float32) * 114.
        else:
            padded_img = np.ones(input_size, np.float32) * 114.
        # resize
        orig_h, orig_w = image.shape[:2]
        r = min(input_size[0] / orig_h, input_size[1] / orig_w)
        resize_size = (int(orig_w * r), int(orig_h * r))
        if r != 1:
            resized_img = cv2.resize(image,
                                     resize_size,
                                     interpolation=cv2.INTER_LINEAR)
        else:
            resized_img = image

        # padding
        padded_img[:resized_img.shape[0], :resized_img.shape[1]] = resized_img

        # [H, W, C] -> [C, H, W]
        padded_img = padded_img.transpose(swap)
        padded_img = np.ascontiguousarray(padded_img, dtype=np.float32)

        return padded_img, r

    def _postprocess(
        self,
        predictions,
        input_size,
        strides,
        num_classes,
        conf_thresh=0.15,
        nms_thresh=0.5,
    ):
        anchors, expand_strides = self._generate_anchors(input_size, strides)
        """
        Input:
            predictions: (ndarray) [n_anchors_all, 4+1+C]
        """
        reg_preds = predictions[..., :4]
        obj_preds = predictions[..., 4:5]
        cls_preds = predictions[..., 5:]
        scores = np.sqrt(obj_preds * cls_preds)

        # scores & class_ids
        class_ids = np.argmax(scores, axis=1)  # [M,]
        scores = scores[(np.arange(scores.shape[0]), class_ids)]  # [M,]

        # bboxes
        bboxes = self._decode_boxes(anchors, reg_preds,
                                    expand_strides)  # [M, 4]

        # thresh
        keep = np.where(scores > conf_thresh)
        scores = scores[keep]
        class_ids = class_ids[keep]
        bboxes = bboxes[keep]

        # nms
        scores, class_ids, bboxes = self._multiclass_nms(
            scores,
            class_ids,
            bboxes,
            nms_thresh,
            num_classes,
            True,
        )

        return bboxes, scores, class_ids

    def _nms(self, bboxes, scores, nms_thresh):
        """"Pure Python NMS."""
        x1 = bboxes[:, 0]  # xmin
        y1 = bboxes[:, 1]  # ymin
        x2 = bboxes[:, 2]  # xmax
        y2 = bboxes[:, 3]  # ymax

        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]

        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            # compute iou
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])

            w = np.maximum(1e-10, xx2 - xx1)
            h = np.maximum(1e-10, yy2 - yy1)
            inter = w * h

            iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-14)
            # reserve all the boundingbox whose ovr less than thresh
            inds = np.where(iou <= nms_thresh)[0]
            order = order[inds + 1]

        return keep

    def _multiclass_nms_class_agnostic(self, scores, labels, bboxes,
                                       nms_thresh):
        # nms
        keep = self._nms(bboxes, scores, nms_thresh)

        scores = scores[keep]
        labels = labels[keep]
        bboxes = bboxes[keep]

        return scores, labels, bboxes

    def _multiclass_nms_class_aware(
        scores,
        labels,
        bboxes,
        nms_thresh,
        num_classes,
    ):
        # nms
        keep = np.zeros(len(bboxes), dtype=np.int)
        for i in range(num_classes):
            inds = np.where(labels == i)[0]
            if len(inds) == 0:
                continue
            c_bboxes = bboxes[inds]
            c_scores = scores[inds]
            c_keep = self._nms(c_bboxes, c_scores, nms_thresh)
            keep[inds[c_keep]] = 1

        keep = np.where(keep > 0)
        scores = scores[keep]
        labels = labels[keep]
        bboxes = bboxes[keep]

        return scores, labels, bboxes

    def _multiclass_nms(
        self,
        scores,
        labels,
        bboxes,
        nms_thresh,
        num_classes,
        class_agnostic=False,
    ):
        if class_agnostic:
            return self._multiclass_nms_class_agnostic(scores, labels, bboxes,
                                                       nms_thresh)
        else:
            return self._multiclass_nms_class_aware(scores, labels, bboxes,
                                                    nms_thresh, num_classes)

    def _generate_anchors(self, input_shape, strides):
        """
            fmp_size: (List) [H, W]
        """
        all_anchors = []
        all_expand_strides = []
        for stride in strides:
            # generate grid cells
            fmp_h, fmp_w = input_shape[0] // stride, input_shape[1] // stride
            anchor_x, anchor_y = np.meshgrid(np.arange(fmp_w),
                                             np.arange(fmp_h))
            # [H, W, 2]
            anchor_xy = np.stack([anchor_x, anchor_y], axis=-1)
            shape = anchor_xy.shape[:2]
            # [H, W, 2] -> [HW, 2]
            anchor_xy = (anchor_xy.reshape(-1, 2) + 0.5) * stride
            all_anchors.append(anchor_xy)

            # expanded stride
            strides = np.full((*shape, 1), stride)
            all_expand_strides.append(strides.reshape(-1, 1))

        anchors = np.concatenate(all_anchors, axis=0)
        expand_strides = np.concatenate(all_expand_strides, axis=0)

        return anchors, expand_strides

    def _decode_boxes(self, anchors, pred_regs, expand_strides):
        """
            anchors:  (List[Tensor]) [1, M, 2] or [M, 2]
            pred_reg: (List[Tensor]) [B, M, 4] or [B, M, 4]
        """
        # center of bbox
        pred_ctr_xy = anchors[..., :2] + pred_regs[..., :2] * expand_strides
        # size of bbox
        pred_box_wh = np.exp(pred_regs[..., 2:]) * expand_strides

        pred_x1y1 = pred_ctr_xy - 0.5 * pred_box_wh
        pred_x2y2 = pred_ctr_xy + 0.5 * pred_box_wh
        pred_box = np.concatenate([pred_x1y1, pred_x2y2], axis=-1)

        return pred_box

    def draw(
        self,
        image,
        score_th,
        bboxes,
        scores,
        class_ids,
        coco_classes,
        thickness=3,
    ):
        debug_image = copy.deepcopy(image)

        for bbox, score, class_id in zip(bboxes, scores, class_ids):
            x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(
                bbox[3])

            if score_th > score:
                continue

            color = self._get_color(class_id)

            # バウンディングボックス
            debug_image = cv2.rectangle(
                debug_image,
                (x1, y1),
                (x2, y2),
                color,
                thickness=thickness,
            )

            # クラスID、スコア
            score = '%.2f' % score
            text = '%s:%s' % (str(coco_classes[int(class_id)]), score)
            debug_image = cv2.putText(
                debug_image,
                text,
                (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                color,
                thickness=thickness,
            )

        return debug_image

    def _get_color(self, index):
        temp_index = abs(int(index + 5)) * 3
        color = (
            (29 * temp_index) % 255,
            (17 * temp_index) % 255,
            (37 * temp_index) % 255,
        )
        return color


if __name__ == '__main__':
    cap = cv2.VideoCapture(0)

    # Load model
    model_path = 'model/yolo_free_nano.onnx'
    model = FreeYOLO(
        model_path,
        providers=[
            'CPUExecutionProvider',
        ],
    )

    # Load COCO Classes List
    with open('coco_classes.txt', 'rt') as f:
        coco_classes = f.read().rstrip('\n').split('\n')

    while True:
        # Capture read
        ret, frame = cap.read()
        if not ret:
            break

        # Inference execution
        bboxes, scores, class_ids = model(frame)

        # Draw
        frame = model.draw(
            frame,
            0.5,
            bboxes,
            scores,
            class_ids,
            coco_classes,
        )

        key = cv2.waitKey(1)
        if key == 27:  # ESC
            break
        cv2.imshow('FreeYOLO', frame)

    cap.release()
    cv2.destroyAllWindows()
