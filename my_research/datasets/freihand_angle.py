# Copyright (c) Xingyu Chen. All Rights Reserved.

"""
 * @file freihand.py
 * @author chenxingyu (chenxy.sean@gmail.com)
 * @brief FreiHAND dataset 
 * @version 0.1
 * @date 2022-04-28
 * 
 * @copyright Copyright (c) 2022 chenxingyu
 * 
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
import torch
import torch.utils.data as data
import numpy as np
from utils.fh_utils import load_db_annotation, read_mesh, read_img, read_img_abs, read_mask_woclip, projectPoints
from utils.vis import base_transform, inv_base_tranmsform, cnt_area
import cv2
from utils.augmentation import Augmentation
from termcolor import cprint
from utils.preprocessing import augmentation, augmentation_2d, trans_point2d
from my_research.tools.kinematics import MPIIHandJoints
from my_research.models.loss import contrastive_loss_3d, contrastive_loss_2d
import vctoolkit as vc
from my_research.build import DATA_REGISTRY
import pandas as pd

@DATA_REGISTRY.register()
class FreiHAND_Angle(data.Dataset):

    def __init__(self, cfg, phase='train', writer=None):
        """Init a FreiHAND Dataset

        Args:
            cfg : config file
            phase (str, optional): train or eval. Defaults to 'train'.
            writer (optional): log file. Defaults to None.
        """
        super(FreiHAND_Angle, self).__init__()
        self.cfg = cfg
        self.phase = phase
        # self.db_data_anno = tuple(load_db_annotation(self.cfg.DATA.FREIHAND.ROOT, set_name=self.phase))
        self.db_data_anno = tuple(load_db_annotation(self.cfg.DATA.FREIHAND.ROOT, set_name='train'))
        self.color_aug = Augmentation() if cfg.DATA.COLOR_AUG and 'train' in self.phase else None

        self.one_version_len = len(self.db_data_anno)
        if 'train' in self.phase:
            self.db_data_anno *= 4
            # get valid image sequences
            self.valid_seq = self._get_valid_seq(os.path.join(cfg.DATA.FREIHAND.ROOT, 'selected_freihand.csv'))
            self.valid_seq_len = len(self.valid_seq)
            # self.valid_seq -> image to use, self.db_data_anno -> overall image data
        if 'test' in self.phase:
            self.valid_seq = self._get_valid_seq(os.path.join(cfg.DATA.FREIHAND.ROOT, 'selected_freihand.csv'))
            self.valid_seq_len = len(self.valid_seq)
        if writer is not None:
            writer.print_str('Loaded FreiHand {} {} samples'.format(self.phase, self.__len__()))
        cprint('Loaded FreiHand {} {} samples'.format(self.phase, self.__len__()), 'red')

    def _get_valid_seq(self, path):
        '''
        return df = [
            [valid_index, its negativeness],
            [1, 1],
            [11, 0],
            [22, 1],
            ...
        ]
        '''
        df = pd.read_csv(path)
        df['Number'] = df['Number'].astype('int')
        df['Negative'] = ((df['Negative'] == 'v') | (df['Negative'] == 'V')).astype('float')
        return df


    def __getitem__(self, idx):  # True
        # ! Do the transform, from torch dataset idx
        # !                   to FreiHAND idx
        freihand_idx = idx // self.valid_seq_len * self.one_version_len
        freihand_idx += self.valid_seq['Number'][idx % self.valid_seq_len]
        negativeness = self.valid_seq['Negative'][idx % self.valid_seq_len]
        # print(f'type: {type(freihand_idx)}, val: {freihand_idx}')

        if 'train' in self.phase or 'test' in self.phase:
            if self.cfg.DATA.CONTRASTIVE and self.phase != 'test':
                return self.get_contrastive_sample(freihand_idx, negativeness=negativeness)
            else:
                return self.get_training_sample(freihand_idx, negativeness=negativeness)
        elif 'eval' in self.phase or 'test' in self.phase:
            return self.get_eval_sample(idx)
        else:
            raise Exception('phase error')

    def get_contrastive_sample(self, idx, negativeness):
        """Get contrastive FreiHAND samples for consistency learning
        """
        # read
        img = read_img_abs(idx, self.cfg.DATA.FREIHAND.ROOT, 'training')
        vert = read_mesh(idx % self.one_version_len, self.cfg.DATA.FREIHAND.ROOT).x.numpy()
        mask = read_mask_woclip(idx % self.one_version_len, self.cfg.DATA.FREIHAND.ROOT, 'training')
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = list(contours)
        contours.sort(key=cnt_area, reverse=True)
        bbox = cv2.boundingRect(contours[0])
        center = [bbox[0]+bbox[2]*0.5, bbox[1]+bbox[3]*0.5]
        w, h = bbox[2], bbox[3]
        bbox = [center[0]-0.5 * max(w, h), center[1]-0.5 * max(w, h), max(w, h), max(w, h)]
        K, mano, joint_cam = self.db_data_anno[idx]
        K, joint_cam, mano = np.array(K), np.array(joint_cam), np.array(mano)
        # print(f'K:\n{K}')
        joint_img = projectPoints(joint_cam, K)
        princpt = K[0:2, 2].astype(np.float32)
        focal = np.array( [K[0, 0], K[1, 1]], dtype=np.float32)
        # multiple aug
        roi_list = []
        calib_list = []
        mask_list = []
        vert_list = []
        joint_cam_list = []
        joint_img_list = []
        aug_param_list = []
        bb2img_trans_list = []
        for _ in range(2):
            # augmentation
            roi, img2bb_trans, bb2img_trans, aug_param, do_flip, scale, roi_mask = augmentation(img.copy(), bbox, self.phase,
                                                                                            exclude_flip=not self.cfg.DATA.FREIHAND.FLIP,
                                                                                            input_img_shape=(self.cfg.DATA.SIZE, self.cfg.DATA.SIZE),
                                                                                            mask=mask.copy(),
                                                                                            base_scale=self.cfg.DATA.FREIHAND.BASE_SCALE,
                                                                                            scale_factor=self.cfg.DATA.FREIHAND.SCALE,
                                                                                            rot_factor=self.cfg.DATA.FREIHAND.ROT,
                                                                                            shift_wh=[bbox[2], bbox[3]],
                                                                                            gaussian_std=self.cfg.DATA.STD)
            if self.color_aug is not None:
                roi = self.color_aug(roi)
            roi = base_transform(roi, self.cfg.DATA.SIZE, mean=self.cfg.DATA.IMG_MEAN, std=self.cfg.DATA.IMG_STD)
            # img = inv_based_tranmsform(roi)
            # cv2.imshow('test', img)
            # cv2.waitKey(0)
            roi = torch.from_numpy(roi).float()
            roi_mask = torch.from_numpy(roi_mask).float()
            bb2img_trans = torch.from_numpy(bb2img_trans).float()
            aug_param = torch.from_numpy(aug_param).float()

            # joints
            joint_img_, princpt_ = augmentation_2d(img, joint_img, princpt, img2bb_trans, do_flip)
            joint_img_ = torch.from_numpy(joint_img_[:, :2]).float() / self.cfg.DATA.SIZE

            # 3D rot
            rot = aug_param[0].item()
            rot_aug_mat = np.array([[np.cos(np.deg2rad(-rot)), -np.sin(np.deg2rad(-rot)), 0],
                                    [np.sin(np.deg2rad(-rot)), np.cos(np.deg2rad(-rot)), 0],
                                    [0, 0, 1]], dtype=np.float32)
            joint_cam_ = torch.from_numpy(np.dot(rot_aug_mat, joint_cam.T).T).float()
            vert_ = torch.from_numpy(np.dot(rot_aug_mat, vert.T).T).float()

            # K
            focal_ = focal * roi.size(1) / (bbox[2]*aug_param[1])
            calib = np.eye(4)
            calib[0, 0] = focal_[0]
            calib[1, 1] = focal_[1]
            calib[:2, 2:3] = princpt_[:, None]
            calib = torch.from_numpy(calib).float()

            roi_list.append(roi)
            mask_list.append(roi_mask.unsqueeze(0))
            calib_list.append(calib)
            vert_list.append(vert_)
            joint_cam_list.append(joint_cam_)
            joint_img_list.append(joint_img_)
            aug_param_list.append(aug_param)
            bb2img_trans_list.append(bb2img_trans)

        # print(f'calib:\n{calib_list[0]}')
        roi = torch.cat(roi_list, 0)
        mask = torch.cat(mask_list, 0)
        calib = torch.cat(calib_list, 0)
        joint_cam = torch.cat(joint_cam_list, -1)
        vert = torch.cat(vert_list, -1)
        joint_img = torch.cat(joint_img_list, -1)
        aug_param = torch.cat(aug_param_list, 0)
        bb2img_trans = torch.cat(bb2img_trans_list, -1)

        # postprocess root and joint_cam
        root = joint_cam[0].clone()
        joint_cam -= root
        vert -= root
        joint_cam /= 0.2
        vert /= 0.2

        # out
        res = {'img': roi, 'joint_img': joint_img, 'joint_cam': joint_cam, 'verts': vert, 'mask': mask,
               'root': root, 'calib': calib, 'aug_param': aug_param, 'bb2img_trans': bb2img_trans,
               'negative': negativeness,}

        return res

    def get_training_sample(self, idx, negativeness):
        """Get a FreiHAND sample for training
        """
        # read
        img = read_img_abs(idx, self.cfg.DATA.FREIHAND.ROOT, 'training')
        vert = read_mesh(idx % self.one_version_len, self.cfg.DATA.FREIHAND.ROOT).x.numpy()
        mask = read_mask_woclip(idx % self.one_version_len, self.cfg.DATA.FREIHAND.ROOT, 'training')
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = list(contours)
        contours.sort(key=cnt_area, reverse=True)
        bbox = cv2.boundingRect(contours[0])
        center = [bbox[0]+bbox[2]*0.5, bbox[1]+bbox[3]*0.5]
        w, h = bbox[2], bbox[3]
        bbox = [center[0]-0.5 * max(w, h), center[1]-0.5 * max(w, h), max(w, h), max(w, h)]
        K, mano, joint_cam = self.db_data_anno[idx]
        K, joint_cam, mano = np.array(K), np.array(joint_cam), np.array(mano)
        joint_img = projectPoints(joint_cam, K)
        princpt = K[0:2, 2].astype(np.float32)
        focal = np.array( [K[0, 0], K[1, 1]], dtype=np.float32)

        # augmentation
        roi, img2bb_trans, bb2img_trans, aug_param, do_flip, scale, mask = augmentation(img, bbox, self.phase,
                                                                                        exclude_flip=not self.cfg.DATA.FREIHAND.FLIP,
                                                                                        input_img_shape=(self.cfg.DATA.SIZE, self.cfg.DATA.SIZE),
                                                                                        mask=mask,
                                                                                        base_scale=self.cfg.DATA.FREIHAND.BASE_SCALE,
                                                                                        scale_factor=self.cfg.DATA.FREIHAND.SCALE,
                                                                                        rot_factor=self.cfg.DATA.FREIHAND.ROT,
                                                                                        shift_wh=[bbox[2], bbox[3]],
                                                                                        gaussian_std=self.cfg.DATA.STD)
        if self.color_aug is not None:
            roi = self.color_aug(roi)
        roi = base_transform(roi, self.cfg.DATA.SIZE, mean=self.cfg.DATA.IMG_MEAN, std=self.cfg.DATA.IMG_STD)
        # img = inv_based_tranmsform(roi)
        # cv2.imshow('test', img)
        # cv2.waitKey(0)
        roi = torch.from_numpy(roi).float()
        mask = torch.from_numpy(mask).float()
        bb2img_trans = torch.from_numpy(bb2img_trans).float()

        # joints
        joint_img, princpt = augmentation_2d(img, joint_img, princpt, img2bb_trans, do_flip)
        joint_img = torch.from_numpy(joint_img[:, :2]).float() / self.cfg.DATA.SIZE

        # 3D rot
        rot = aug_param[0]
        rot_aug_mat = np.array([[np.cos(np.deg2rad(-rot)), -np.sin(np.deg2rad(-rot)), 0],
                                [np.sin(np.deg2rad(-rot)), np.cos(np.deg2rad(-rot)), 0],
                                [0, 0, 1]], dtype=np.float32)
        ''' 拇指朝 z+ 方向旋轉 (-rot) 度
        | cos(-rot) | -sin(-rot) | 0 |
        | sin(-rot) |  cos(-rot) | 0 |
        |     0     |    0       | 1 |

        因為 內部的 rot 是指旋轉 bbox! 旋轉後的 bbox 再經由 affine trans 轉到輸出圖片座標時，事實上做的旋轉是倒過來的
        '''
        joint_cam = np.dot(rot_aug_mat, joint_cam.T).T
        vert = np.dot(rot_aug_mat, vert.T).T

        # K
        focal = focal * roi.size(1) / (bbox[2]*aug_param[1])
        calib = np.eye(4)
        calib[0, 0] = focal[0]
        calib[1, 1] = focal[1]
        calib[:2, 2:3] = princpt[:, None]
        calib = torch.from_numpy(calib).float()

        # postprocess root and joint_cam
        root = joint_cam[0].copy()
        joint_cam -= root
        vert -= root
        joint_cam /= 0.2
        vert /= 0.2
        root = torch.from_numpy(root).float()
        joint_cam = torch.from_numpy(joint_cam).float()
        vert = torch.from_numpy(vert).float()

        # out
        res = {'img': roi, 'joint_img': joint_img, 'joint_cam': joint_cam, 'verts': vert,
               'mask': mask, 'root': root, 'calib': calib,
               'negative': negativeness,
              }

        return res

    def get_eval_sample(self, idx):
        """Get FreiHAND sample for evaluation
        """
        # read
        img = read_img(idx, self.cfg.DATA.FREIHAND.ROOT, 'evaluation', 'gs')
        K, scale = self.db_data_anno[idx]
        K = np.array(K)
        princpt = K[0:2, 2].astype(np.float32)
        focal = np.array( [K[0, 0], K[1, 1]], dtype=np.float32)
        bbox = [img.shape[1]//2-50, img.shape[0]//2-50, 100, 100]  # img: (224, 224), bbox=[62, 62, 100, 100]
        center = [bbox[0]+bbox[2]*0.5, bbox[1]+bbox[3]*0.5]
        w, h = bbox[2], bbox[3]
        bbox = [center[0]-0.5 * max(w, h), center[1]-0.5 * max(w, h), max(w, h), max(w, h)]  # square(left, top, w, h)

        # aug
        roi, img2bb_trans, bb2img_trans, aug_param, do_flip, scale, _ = augmentation(img, bbox, self.phase,
                                                                                        exclude_flip=not self.cfg.DATA.FREIHAND.FLIP,
                                                                                        input_img_shape=(self.cfg.DATA.SIZE, self.cfg.DATA.SIZE),
                                                                                        mask=None,
                                                                                        base_scale=self.cfg.DATA.FREIHAND.BASE_SCALE,
                                                                                        scale_factor=self.cfg.DATA.FREIHAND.SCALE,
                                                                                        rot_factor=self.cfg.DATA.FREIHAND.ROT,
                                                                                        shift_wh=[bbox[2], bbox[3]],
                                                                                        gaussian_std=self.cfg.DATA.STD)
        # aug_param: [旋轉徑度, bbox 放大倍率(框到更多手外圍的圖), 平移 x 佔畫面比例, 平移 y 佔畫面比例]
        roi = base_transform(roi, self.cfg.DATA.SIZE, mean=self.cfg.DATA.IMG_MEAN, std=self.cfg.DATA.IMG_STD)
        roi = torch.from_numpy(roi).float()

        # s     : 放大倍率 = roi.size(1) / (bbox[2]*aug_param[1])
        # Um, Vm: 平移距離 = roi.size(1) * aug_param[2], ...[3]
        #                                平移 x 佔整張圖的比例
        # Evaluation 時，不會有 shift，只會有 bbox 的移動

        # Joint, augmentation_2d, just like get_training_sample() do
        princpt = trans_point2d(princpt, img2bb_trans)

        # K
        focal = focal * roi.size(1) / (bbox[2]*aug_param[1])  # 放大倍率為：result(roi) / origin(bbox*scale, 擴大 bbox 擷取框框的部份)
        calib = np.eye(4)
        calib[0, 0] = focal[0]
        calib[1, 1] = focal[1]
        calib[:2, 2:3] = princpt[:, None]
        # print(f'K:\n{K}')
        # print(f'calib:\n{calib}')
        calib = torch.from_numpy(calib).float()

        return {'img': roi, 'calib': calib, 'idx': idx}

    def __len__(self):
        if self.phase == 'train':
            return len(self.valid_seq) * 4
        else:
            return len(self.valid_seq)

    def visualization(self, res, idx):
        """Visualization of correctness
        """
        import matplotlib.pyplot as plt
        from my_research.tools.vis import perspective
        # num_sample = (1, 2)[self.cfg.DATA.CONTRASTIVE]
        num_sample = 1
        for i in range(num_sample):
            fig = plt.figure(figsize=(8, 2))
            img = inv_base_tranmsform(res['img'].numpy()[i*3:(i+1)*3])
            # joint_img
            if 'joint_img' in res:
                ax = plt.subplot(1, 4, 1)
                vis_joint_img = vc.render_bones_from_uv(np.flip(res['joint_img'].numpy()[:, i*2:(i+1)*2]*self.cfg.DATA.SIZE, axis=-1).copy(),
                                                        img.copy(), MPIIHandJoints, thickness=2)
                ax.imshow(vis_joint_img)
                ax.set_title('kps2d')
                ax.axis('off')
            # aligned joint_cam
            if 'joint_cam' in res:
                ax = plt.subplot(1, 4, 2)
                xyz = res['joint_cam'].numpy()[:, i*3:(i+1)*3].copy()
                root = res['root'].numpy()[i*3:(i+1)*3].copy()
                xyz = xyz * 0.2 + root
                proj3d = perspective(torch.from_numpy(xyz.copy()).permute(1, 0).unsqueeze(0), res['calib'][i*4:(i+1)*4].unsqueeze(0))[0].numpy().T
                vis_joint_img = vc.render_bones_from_uv(np.flip(proj3d[:, :2], axis=-1).copy(),
                                                        img.copy(), MPIIHandJoints, thickness=2)
                ax.imshow(vis_joint_img)
                ax.set_title('kps3d2d')
                ax.axis('off')
            # aligned verts
            if 'verts' in res:
                ax = plt.subplot(1, 4, 3)
                vert = res['verts'].numpy()[:, i*3:(i+1)*3].copy()
                vert = vert * 0.2 + root
                proj_vert = perspective(torch.from_numpy(vert.copy()).permute(1, 0).unsqueeze(0), res['calib'][i*4:(i+1)*4].unsqueeze(0))[0].numpy().T
                ax.imshow(img)
                plt.plot(proj_vert[:, 0], proj_vert[:, 1], 'o', color='red', markersize=1)
                ax.set_title('verts')
                ax.axis('off')
            # mask
            if 'mask' in res:
                ax = plt.subplot(1, 4, 4)
                if res['mask'].ndim == 3:
                    mask = res['mask'].numpy()[i] * 255
                else:
                    mask = res['mask'].numpy() * 255
                mask_ = np.concatenate([mask[:, :, None]] + [np.zeros_like(mask[:, :, None])] * 2, 2).astype(np.uint8)
                img_mask = cv2.addWeighted(img, 1, mask_, 0.5, 1)
                ax.imshow(img_mask)
                ax.set_title('mask')
                ax.axis('off')
            # plt.title(f'Thumb negativeness: {res["negative"]}, idx%len: {idx % self.valid_seq_len}')
            fig.suptitle(f'Thumb negativeness: {res["negative"]}, idx%len: {idx % self.valid_seq_len}')
            plt.show()
        if self.cfg.DATA.CONTRASTIVE:
            aug_param = res['aug_param'].unsqueeze(0)
            vert = res['verts'].unsqueeze(0)
            joint_img = res['joint_img'].unsqueeze(0)
            uv_trans = res['bb2img_trans'].unsqueeze(0)
            loss3d = contrastive_loss_3d(vert, aug_param)
            loss2d = contrastive_loss_2d(joint_img, uv_trans, res['img'].size(2))
            print(idx, loss3d, loss2d)


if __name__ == '__main__':
    """Test the dataset
    """
    from my_research.main import setup
    from options.cfg_options import CFGOptions

    args = CFGOptions().parse()
    args.config_file = 'my_research/configs/mobrecon_rs.yml'
    cfg = setup(args)

    dataset = FreiHAND_Angle(cfg, 'train')
    for i in range(dataset.valid_seq_len, dataset.valid_seq_len+5):
        e = dataset[i]
        dataset.visualization(e, i)

    # from tqdm import tqdm
    # for i in tqdm(range(41705, len(dataset))):
    #     a = dataset[i]['negative']

    # dataset[39183] -> 132225 | max==130240
    # for i in range(0, len(dataset), len(dataset)//10):
    #     print(i)
    #     data = dataset.__getitem__(i)
    #     dataset.visualization(data, i)

    # loader = data.DataLoader(dataset, batch_size=32)
    # for b in loader:
    #     print(b['negative'].shape)  # [32]
    for i in range(10542, 10547):
        print(dataset[i]['negative'])
    # idx = 0
    # data = dataset[idx]
    # dataset.visualization(data, idx)
