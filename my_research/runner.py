import os
import numpy as np
import time
import torch
import cv2
import json
from utils.warmup_scheduler import adjust_learning_rate
from utils.vis import inv_base_tranmsform
from utils.zimeval import EvalUtil
from utils.transforms import rigid_align
from my_research.tools.vis import perspective, compute_iou, cnt_area
from my_research.tools.kinematics import mano_to_mpii, MPIIHandJoints
from my_research.tools.registration import registration
import vctoolkit as vc


class Runner(object):
    def __init__(self, cfg, args, model, train_loader, val_loader, test_loader, optimizer, writer, device, board, start_epoch=0):
        super(Runner, self).__init__()
        self.cfg = cfg
        self.args = args
        self.model = model
        face = np.load(os.path.join(cfg.MODEL.MANO_PATH, 'right_faces.npy'))
        self.face = torch.from_numpy(face).long()
        self.j_reg = np.load(os.path.join(self.cfg.MODEL.MANO_PATH, 'j_reg.npy'))
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.test_loader = test_loader
        self.max_epochs = cfg.TRAIN.EPOCHS
        self.optimizer = optimizer
        self.writer = writer
        self.device = device
        self.board = board
        self.start_epoch = start_epoch
        self.epoch = max(start_epoch - 1, 0)
        if cfg.PHASE == 'train':
            self.total_step = self.start_epoch * (len(self.train_loader.dataset) // cfg.TRAIN.BATCH_SIZE)
            try:
                self.loss = self.model.loss
            except:
                self.loss = self.model.module.loss
        self.best_val_loss = np.float('inf')
        print('runner init done')

    def run(self):
        if self.cfg.PHASE == 'train':
            if self.val_loader is not None and self.epoch > 0:
                self.best_val_loss = self.eval()
            for epoch in range(self.start_epoch, self.max_epochs + 1):
                self.epoch = epoch
                t = time.time()
                if self.args.world_size > 1:
                    self.train_loader.sampler.set_epoch(epoch)
                train_loss = self.train()
                t_duration = time.time() - t
                if self.val_loader is not None:
                    val_loss = self.eval()
                else:
                    val_loss = np.float('inf')

                info = {
                    'current_epoch': self.epoch,
                    'epochs': self.max_epochs,
                    'train_loss': train_loss,
                    'test_loss': val_loss,
                    't_duration': t_duration
                }
                print(f'epoch: {self.epoch: 3} / {self.max_epochs}')

                self.writer.print_info(info)
                if val_loss < self.best_val_loss:
                    self.writer.save_checkpoint(self.model, self.optimizer, None, self.epoch, best=True)
                    self.best_test_loss = val_loss
                # if self.epoch in [5, 10]:
                #     self.writer.save_checkpoint(self.model, self.optimizer, None, self.epoch)
                self.writer.save_checkpoint(self.model, self.optimizer, None, self.epoch, last=True)

            self.pred() if self.cfg.TRAIN.DATASET != 'FreiHAND_Angle' else self.pred_negative()
        elif self.cfg.PHASE == 'eval':
            self.eval()
        elif self.cfg.PHASE == 'pred':
            if self.cfg.TRAIN.DATASET == 'FreiHAND_Angle':  # output negative head result
                self.pred_negative()
            else:
                self.pred()
        elif self.cfg.PHASE == 'demo':
            self.demo()
        else:
            raise Exception('PHASE ERROR')

    def phrase_data(self, data):
        '''
        將 data[each key] 中的每一筆 data 都傳到 .to('GPU') 裡面
        '''
        for key, val in data.items():
            try:
                if isinstance(val, list):
                    data[key] = [d.to(self.device) for d in data[key]]
                else:
                    data[key] = data[key].to(self.device)
            except:
                pass
        return data

    def board_scalar(self, phase, n_iter, lr=None, **kwargs):
        split = '/'
        for key, val in kwargs.items():
            if 'loss' in key:
                if isinstance(val, torch.Tensor):
                    val = val.item()
                self.board.add_scalar(phase + split + key, val, n_iter)
            if lr:
                self.board.add_scalar(phase + split + 'lr', lr, n_iter)

    def draw_results(self, data, out, loss, batch_id, aligned_verts=None):
        img_cv2 = inv_base_tranmsform(data['img'][batch_id].cpu().numpy())[..., :3]
        draw_list = []
        if 'joint_img' in data:
            draw_list.append( vc.render_bones_from_uv(np.flip(data['joint_img'][batch_id, :, :2].cpu().numpy()*self.cfg.DATA.SIZE, axis=-1).copy(),
                                                      img_cv2.copy(), MPIIHandJoints, thickness=2) )
        if 'joint_img' in out:
            try:
                draw_list.append( vc.render_bones_from_uv(np.flip(out['joint_img'][batch_id, :, :2].detach().cpu().numpy()*self.cfg.DATA.SIZE, axis=-1).copy(),
                                                         img_cv2.copy(), MPIIHandJoints, thickness=2) )
            except:
                draw_list.append(img_cv2.copy())
        if 'root' in data:
            root = data['root'][batch_id:batch_id+1, :3]
        else:
            root = torch.FloatTensor([[0, 0, 0.6]]).to(data['img'].device)
        if 'verts' in data:
            vis_verts_gt = img_cv2.copy()
            verts = data['verts'][batch_id:batch_id+1, :, :3] * 0.2 + root
            vp = perspective(verts.permute(0, 2, 1), data['calib'][batch_id:batch_id+1, :4])[0].cpu().numpy().T
            for i in range(vp.shape[0]):
                cv2.circle(vis_verts_gt, (int(vp[i, 0]), int(vp[i, 1])), 1, (255, 0, 0), -1)
            draw_list.append(vis_verts_gt)
        if 'verts' in out:
            try:
                vis_verts_pred = img_cv2.copy()
                if aligned_verts is None:
                    verts = out['verts'][batch_id:batch_id+1, :, :3] * 0.2 + root
                else:
                    verts = aligned_verts
                vp = perspective(verts.permute(0, 2, 1), data['calib'][batch_id:batch_id+1, :4])[0].detach().cpu().numpy().T
                for i in range(vp.shape[0]):
                    cv2.circle(vis_verts_pred, (int(vp[i, 0]), int(vp[i, 1])), 1, (255, 0, 0), -1)
                draw_list.append(vis_verts_pred)
            except:
                draw_list.append(img_cv2.copy())

        return np.concatenate(draw_list, 1)

    def board_img(self, phase, n_iter, data, out, loss, batch_id=0):
        draw = self.draw_results(data, out, loss, batch_id)
        self.board.add_image(phase + '/res', draw.transpose(2, 0, 1), n_iter)

    def train(self):
        self.writer.print_str('TRAINING ..., Epoch {}/{}'.format(self.epoch, self.max_epochs))
        self.model.train()
        if self.cfg.TRAIN.DATASET == 'FreiHAND_Angle':
            # freeze operation for BN.running_mean... in negative predictor
            # and was finally useless
            # if self.epoch < 65:
            #     self.model._eval_bn_layers()
            pass

        total_loss = 0
        forward_time = 0.
        backward_time = 0.
        start_time = time.time()
        for step, data in enumerate(self.train_loader):
            ts = time.time()
            adjust_learning_rate(self.optimizer, self.epoch, step, len(self.train_loader), self.cfg.TRAIN.LR, self.cfg.TRAIN.LR_DECAY, self.cfg.TRAIN.DECAY_STEP, self.cfg.TRAIN.WARMUP_EPOCHS)
            data = self.phrase_data(data)  # to('GPU')
            self.optimizer.zero_grad()
            out = self.model(data['img'])
            tf = time.time()
            forward_time += tf - ts
            losses = self.loss(verts_pred=out.get('verts'),
                               joint_img_pred=out['joint_img'],
                               joint_conf_pred=out.get('joint_conf'),  # append conf prediciton
                               joint_3d_pred=out.get('joints'),    # append joint prediction

                               verts_gt=data.get('verts'),
                               joint_img_gt=data['joint_img'],
                               joint_3d_gt=data.get('joint_cam'),  # append joint root-relative

                               negative_pred=out.get('negative'),  # supplement for negative thumb
                               negative_gt=data.get('negative'),

                               face=self.face,
                               aug_param=(None, data.get('aug_param'))[self.epoch>4],
                               bb2img_trans=data.get('bb2img_trans'),
                               size=data['img'].size(2),
                               mask_gt=data.get('mask'),
                               trans_pred=out.get('trans'),
                               alpha_pred=out.get('alpha'),
                               img=data.get('img'))
            loss = losses['loss']
            loss.backward()
            self.optimizer.step()
            tb = time.time()
            backward_time +=  tb - tf

            self.total_step += 1
            total_loss += loss.item()
            if self.board is not None:
                self.board_scalar('train', self.total_step, self.optimizer.param_groups[0]['lr'], **losses)
            if self.total_step % 100 == 0:
                cur_time = time.time()
                duration = cur_time - start_time
                start_time = cur_time
                info = {
                    'train_loss': loss.item(),
                    'l1_loss': losses.get('verts_loss', 0),
                    'epoch': self.epoch,
                    'max_epoch': self.max_epochs,
                    'step': step,
                    'max_step': len(self.train_loader),
                    'total_step': self.total_step,
                    'step_duration': duration,
                    'forward_duration': forward_time,
                    'backward_duration': backward_time,
                    'lr': self.optimizer.param_groups[0]['lr']
                }
                self.writer.print_step_ft(info)
                forward_time = 0.
                backward_time = 0.

        if self.board is not None:
            self.board_img('train', self.epoch, data, out, losses)

        return total_loss / len(self.train_loader)

    def eval(self):
        self.writer.print_str('EVALING ... Epoch {}/{}'.format(self.epoch, self.max_epochs))
        self.model.eval()
        evaluator_2d = EvalUtil()
        evaluator_rel = EvalUtil()
        evaluator_pa = EvalUtil()
        mask_iou = []
        joint_cam_errors = []
        pa_joint_cam_errors = []
        joint_img_errors = []
        with torch.no_grad():
            for step, data in enumerate(self.val_loader):
                if self.board is None and step % 100 == 0:
                    print(step, len(self.val_loader))
                # get data then infernce
                data = self.phrase_data(data)
                out = self.model(data['img'])

                # get vertex pred
                verts_pred = out['verts'][0].cpu().numpy() * 0.2
                joint_cam_pred = mano_to_mpii(np.matmul(self.j_reg, verts_pred)) * 1000.0

                # get mask pred
                mask_pred = out.get('mask')
                if mask_pred is not None:
                    mask_pred = (mask_pred[0] > 0.3).cpu().numpy().astype(np.uint8)
                    mask_pred = cv2.resize(mask_pred, (data['img'].size(3), data['img'].size(2)))
                else:
                    mask_pred = np.zeros((data['img'].size(3), data['img'].size(2)), np.uint8)

                # get uv pred
                joint_img_pred = out.get('joint_img')
                if joint_img_pred is not None:
                    joint_img_pred = joint_img_pred[0].cpu().numpy() * data['img'].size(2)
                else:
                    joint_img_pred = np.zeros((21, 2), dtype=np.float)

                # pck
                joint_cam_gt = data['joint_cam'][0].cpu().numpy() * 1000.0
                joint_cam_align = rigid_align(joint_cam_pred, joint_cam_gt)
                evaluator_2d.feed(data['joint_img'][0].cpu().numpy() * data['img'].size(2), joint_img_pred)
                evaluator_rel.feed(joint_cam_gt, joint_cam_pred)
                evaluator_pa.feed(joint_cam_gt, joint_cam_align)

                # error
                if 'mask_gt' in data.keys():
                    mask_iou.append(compute_iou(mask_pred, cv2.resize(data['mask_gt'][0].cpu().numpy(), (data['img'].size(3), data['img'].size(2)))))
                else:
                    mask_iou.append(0)
                joint_cam_errors.append(np.sqrt(np.sum((joint_cam_pred - joint_cam_gt) ** 2, axis=1)))
                pa_joint_cam_errors.append(np.sqrt(np.sum((joint_cam_gt - joint_cam_align) ** 2, axis=1)))
                joint_img_errors.append(np.sqrt(np.sum((data['joint_img'][0].cpu().numpy()*data['img'].size(2) - joint_img_pred) ** 2, axis=1)))

            # get auc
            _1, _2, _3, auc_rel, pck_curve_rel, thresholds2050 = evaluator_rel.get_measures(20, 50, 20)
            _1, _2, _3, auc_pa, pck_curve_pa, _ = evaluator_pa.get_measures(20, 50, 20)
            _1, _2, _3, auc_2d, pck_curve_2d, _ = evaluator_2d.get_measures(0, 30, 20)
            # get error
            miou = np.array(mask_iou).mean()
            mpjpe = np.array(joint_cam_errors).mean()
            pampjpe = np.array(pa_joint_cam_errors).mean()
            uve = np.array(joint_img_errors).mean()

            if self.board is not None:
                self.board_scalar('test', self.epoch, **{'auc_loss': auc_rel, 'pa_auc_loss': auc_pa, '2d_auc_loss': auc_2d, 'mIoU_loss': miou, 'uve': uve, 'mpjpe_loss': mpjpe, 'pampjpe_loss': pampjpe})
                self.board_img('test', self.epoch, data, out, {})
            elif self.args.world_size < 2:
                print( f'pampjpe: {pampjpe}, mpjpe: {mpjpe}, uve: {uve}, miou: {miou}, auc_rel: {auc_rel}, auc_pa: {auc_pa}, auc_2d: {auc_2d}')
                print('thresholds2050', thresholds2050)
                print('pck_curve_all_pa', pck_curve_pa)
            self.writer.print_str( f'pampjpe: {pampjpe}, mpjpe: {mpjpe}, uve: {uve}, miou: {miou}, auc_rel: {auc_rel}, auc_pa: {auc_pa}, auc_2d: {auc_2d}')

        return pampjpe

    def pred(self):
        self.writer.print_str('PREDICING ... Epoch {}/{}'.format(self.epoch, self.max_epochs))
        self.model.eval()
        xyz_pred_list, verts_pred_list = list(), list()
        with torch.no_grad():
            for step, data in enumerate(self.test_loader):
                if self.board is None and step % 100 == 0:
                    print(step, len(self.test_loader))
                # print(f'Eval on image[{data["idx"].cpu()}]')
                data = self.phrase_data(data)
                out = self.model(data['img'])
                # EXP
                # print(f'input      : {data["img"].size()}')         # (1, 3, 128, 128)
                # print(f'output:')
                # print(f'       vert: {out["verts"].size()}')        # (1, 778, 3)
                # print(f'  joint_img: {out["joint_img"].size()}')    # (1, 21, 2)
                # data['img'].permute((0, 2, 3, 1)).reshape((128, 128, 3))
                # np.save('EXP_pred/img_new.npy', data['img'].permute((0, 2, 3, 1)).reshape((128, 128, 3)).cpu().numpy())
                # np.save('EXP_pred/out_vert_new.npy', out['verts'][0].cpu().numpy())
                # np.save('EXP_pred/out_joint_new.npy', out['joint_img'][0].cpu().numpy())

                # np.save('EXP_pred/regressor_new', self.j_reg)
                # return

                # get verts pred
                verts_pred = out['verts'][0].cpu().numpy() * 0.2 # old line: 195

                # get mask pred
                mask_pred = out.get('mask')
                if mask_pred is not None:
                    mask_pred = (mask_pred[0] > 0.3).cpu().numpy().astype(np.uint8)
                    mask_pred = cv2.resize(mask_pred, (data['img'].size(3), data['img'].size(2)))
                    try:
                        contours, _ = cv2.findContours(mask_pred, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        contours.sort(key=cnt_area, reverse=True)
                        poly = contours[0].transpose(1, 0, 2).astype(np.int32)
                    except:
                        poly = None
                else:
                    poly = None

                # get uv pred
                joint_img_pred = out.get('joint_img')
                if joint_img_pred is not None:
                    joint_img_pred = joint_img_pred[0].cpu().numpy() * data['img'].size(2)
                    verts_pred, align_state = registration(verts_pred, joint_img_pred, self.j_reg, data['calib'][0].cpu().numpy(), self.cfg.DATA.SIZE, poly=poly)

                # get joint_cam
                joint_cam_pred = mano_to_mpii(np.matmul(self.j_reg, verts_pred))

                # np.save('EXP_pred/joint_cam_pred_new.npy', joint_cam_pred)
                # np.save('EXP_pred/verts_pred_new.npy', verts_pred)
                # raise Exception('Hello runner')
                # track data
                xyz_pred_list.append(joint_cam_pred)
                verts_pred_list.append(verts_pred)
                if self.cfg.TEST.SAVE_PRED:
                    draw = self.draw_results(data, out, {}, 0, aligned_verts=torch.from_numpy(verts_pred).float()[None, ...])[..., ::-1]
                    cv2.imwrite(os.path.join(self.args.out_dir, self.cfg.TEST.SAVE_DIR, f'{step}.png'), draw)

        # dump results
        xyz_pred_list = [x.tolist() for x in xyz_pred_list]
        verts_pred_list = [x.tolist() for x in verts_pred_list]
        # save to a json
        with open(os.path.join(self.args.out_dir, f'{self.args.exp_name}.json'), 'w') as fo:
            json.dump(
                [
                    xyz_pred_list,
                    verts_pred_list
                ], fo)
        self.writer.print_str('Dumped %d joints and %d verts predictions to %s' % (
            len(xyz_pred_list), len(verts_pred_list), os.path.join(self.args.work_dir, 'out', self.args.exp_name, f'{self.args.exp_name}.json')))

    def pred_negative(self):
        self.writer.print_str('PREDICING ... Epoch {}/{}'.format(self.epoch, self.max_epochs))
        self.model.eval()
        from utils.vis import registration, map2uv, inv_base_tranmsform
        from utils.draw3d import save_a_image_with_mesh_joints
        from utils.read import save_mesh
        self.set_demo(self.args)

        InferenceTestingImages = False  # <- Hyper Parameter
        HeadCount = 1  # 2nd
        heads_negativeness = [[] for _ in range(HeadCount+1)]
        counter = 0
        with torch.no_grad():
            for step, data in enumerate(self.test_loader):
                if self.board is None and step % 100 == 0:
                    print(step, len(self.test_loader))
                data = self.phrase_data(data)
                out = self.model(data['img'])

                # out['negative'].shape = (B, 2), 2 heads
                for i in range(HeadCount):
                    heads_negativeness[i] += [torch.sigmoid(out['negative'][0, i]).cpu().numpy()]
                heads_negativeness[HeadCount] += [data['negative'][0].cpu().numpy()]

                if torch.mean(torch.sigmoid(out['negative'][0, 0])) > 0.5:
                    if data['negative'][0] == 1:
                        counter += 1

                if InferenceTestingImages:
                    image = inv_base_tranmsform(data['img'][0].cpu().numpy())
                    mask_pred = np.zeros([data['img'].size(3), data['img'].size(2)])
                    poly = None
                    # vertex
                    pred = out['verts'][0] if isinstance(out['verts'], list) else out['verts']
                    vertex = (pred[0].cpu() * self.std.cpu()).numpy()
                    uv_pred = out['joint_img']
                    K = data['calib'][0][:3, :3].cpu().numpy()
                    if uv_pred.ndim == 4:
                        uv_point_pred, uv_pred_conf = map2uv(uv_pred.cpu().numpy(), (data['img'].size(2), data['img'].size(3)))
                    else:
                        uv_point_pred, uv_pred_conf = (uv_pred * data['img'].size(2)).cpu().numpy(), [None,]
                    vertex, align_state = registration(vertex, uv_point_pred[0], self.j_regressor, K, data['img'].size(2), uv_conf=uv_pred_conf[0], poly=poly)

                    vertex2xyz = mano_to_mpii(np.matmul(self.j_regressor, vertex))

                    save_a_image_with_mesh_joints(image[..., ::-1], mask_pred, poly, K, vertex, self.face, uv_point_pred[0], vertex2xyz,
                                                os.path.join(self.args.out_dir, 'test', f'{step+2:06}' + '_plot.jpg'))
        negative_np = np.array(heads_negativeness).transpose()
        negative_path = os.path.join(self.args.out_dir, f'negative.csv')
        np.savetxt(negative_path, negative_np, delimiter=',')
        print(f'{counter} / {len(self.test_loader)}...')

    def set_demo(self, args):
        import pickle
        with open(os.path.join(args.work_dir, '../template/MANO_RIGHT.pkl'), 'rb') as f:
            mano = pickle.load(f, encoding='latin1')
        self.j_regressor = np.zeros([21, 778])
        self.j_regressor[:16] = mano['J_regressor'].toarray()
        for k, v in {16: 333, 17: 444, 18: 672, 19: 555, 20: 744}.items():
            self.j_regressor[k, v] = 1
        self.std = torch.tensor(0.20)

    def demo(self):
        from utils.progress.bar import Bar
        from termcolor import colored
        from utils.vis import registration, map2uv, base_transform
        from utils.draw3d import save_a_image_with_mesh_joints
        from utils.read import save_mesh
        self.set_demo(self.args)

        # INFER_LIST = ['01M', '01R', '01U', '03M', '03R', '03U', '05M', '05R', '05U', '07M', '07R', '07U', '09M', '09R', '09U', '21M', '21R', '21U', '23M', '23R', '23U', '25M', '25R', '25U', '27M', '27R', '27U', '29M', '29R', '29U', '41M', '41R', '41U', '43M', '43R', '43U', '45M', '45R', '45U', '47M', '47R', '47U', '49M', '49R', '49U']
        INFER_LIST = [e for e in os.listdir(os.path.join(self.args.work_dir, 'images'))]
        INFER_LIST.remove('default.npy')
        INFER_LIST.remove('.gitignore')

        for i in range(len(INFER_LIST)):
            INFER_FOLDER = INFER_LIST[i]
            print(f'Predicting {INFER_FOLDER}')

            args = self.args
            args.size = 128  # NEW APPEND
            self.model.eval()
            # image_fp = os.path.join(args.work_dir, 'images')
            image_fp = os.path.join(args.work_dir, 'images', INFER_FOLDER)
            output_fp = os.path.join(args.out_dir, 'demo', INFER_FOLDER)
            os.makedirs(output_fp, exist_ok=True)
            ''' paths
            input : ~/HandMesh/images/{INFER_FOLDER}/
            output: ~/HandMesh/out/FreiHAND/mrc_ds/demo/{INFER_FOLDER} /
            '''

            # image_files = [os.path.join(image_fp, i) for i in os.listdir(image_fp) if '_img.jpg' in i]
            image_files = [os.path.join(image_fp, e) for e in os.listdir(image_fp) if e.endswith('.jpg')]  # or jpg...
            bar = Bar(colored("DEMO", color='blue'), max=len(image_files))
            with torch.no_grad():
                negativeness = []  # probability: (0 ~ 1)
                for step, image_path in enumerate(image_files):
                    # EXP
                    # print('TPYE', type(self.face))
                    # np.save('EXP_demo/face.npy', self.face.cpu().detach().numpy())
                    # return
                    # print(f'Demo on: {image_path}')
                    # image_path = '/home/oscar/Desktop/HandMesh/my_research/images/0_stone/image.jpg'
                    # EXP

                    # image_name = image_path.split('/')[-1].split('_')[0]
                    image_name = os.path.basename(image_path).split('.')[0]  # '0000'
                    image = cv2.imread(image_path)[..., ::-1]
                    image = cv2.resize(image, (args.size, args.size))
                    input = torch.from_numpy(base_transform(image, size=args.size)).unsqueeze(0).to(self.device)

                    # print(f'processing file: {image_path}')
                    _Knpy_file_path = image_path.replace('_img.jpg', '_K.npy')
                    if os.path.isfile(_Knpy_file_path) and _Knpy_file_path.endswith('_K.npy'):  # example images' K
                        K = np.load(_Knpy_file_path)
                    elif os.path.isfile(os.path.join(args.work_dir, 'images', 'default.npy')):  # my images' K
                        K = np.load(os.path.join(args.work_dir, 'images', 'default.npy'))

                    K[0, 0] = K[0, 0] / 224 * args.size
                    K[1, 1] = K[1, 1] / 224 * args.size
                    K[0, 2] = args.size // 2
                    K[1, 2] = args.size // 2

                    out = self.model(input)
                    # print(f'input      : {input.size()}')         # (1, 3, 128, 128)
                    # print(f'output:')
                    # print(f'       vert: {out["verts"].size()}')        # (1, 778, 3)
                    # print(f'  joint_img: {out["joint_img"].size()}')    # (1, 21, 2)
                    # np.save('EXP_demo/in.npy', input.cpu().detach().numpy())
                    # np.save('EXP_demo/vert.npy', out['verts'][0].cpu().detach().numpy())
                    # np.save('EXP_demo/joint.npy', out['joint_img'][0].cpu().detach().numpy())
                    # return
                    # silhouette
                    mask_pred = out.get('mask_pred')  # edited here
                    if mask_pred is not None:
                        mask_pred = (mask_pred[0] > 0.3).cpu().numpy().astype(np.uint8)
                        mask_pred = cv2.resize(mask_pred, (input.size(3), input.size(2)))
                        try:
                            contours, _ = cv2.findContours(mask_pred, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                            contours.sort(key=cnt_area, reverse=True)
                            poly = contours[0].transpose(1, 0, 2).astype(np.int32)
                        except:
                            poly = None
                    else:
                        mask_pred = np.zeros([input.size(3), input.size(2)])
                        poly = None
                    # vertex
                    pred = out['verts'][0] if isinstance(out['verts'], list) else out['verts']
                    vertex = (pred[0].cpu() * self.std.cpu()).numpy()
                    uv_pred = out['joint_img']
                    if uv_pred.ndim == 4:
                        uv_point_pred, uv_pred_conf = map2uv(uv_pred.cpu().numpy(), (input.size(2), input.size(3)))
                    else:
                        uv_point_pred, uv_pred_conf = (uv_pred * args.size).cpu().numpy(), [None,]
                    vertex, align_state = registration(vertex, uv_point_pred[0], self.j_regressor, K, args.size, uv_conf=uv_pred_conf[0], poly=poly)

                    vertex2xyz = mano_to_mpii(np.matmul(self.j_regressor, vertex))
                    # np.savetxt(os.path.join(args.out_dir, 'demotext', image_name + '_xyz.txt'), vertex2xyz)
                    # np.savetxt(os.path.join(output_fp, image_name + '_xyz.txt'), vertex2xyz, fmt='%f')
                    np.save(os.path.join(output_fp, image_name + '_xyz.npy'), vertex2xyz)

                    save_a_image_with_mesh_joints(image[..., ::-1], mask_pred, poly, K, vertex, self.face, uv_point_pred[0], vertex2xyz,
                                                os.path.join(output_fp, image_name + '_plot.jpg'))
                    save_mesh(os.path.join(output_fp, image_name + '_mesh.ply'), vertex, self.face)
                    # faces is incorrect

                    if out.get('negative') is not None:
                        negativeness += [
                            # torch.sigmoid(torch.mean(out['negative'][0, :]).cpu())  # [B, heads]
                            torch.sigmoid(out['negative'][0, 0]).cpu()
                            ]  # apply sigmoid on model out, same to loss calculation

                    bar.suffix = '({batch}/{size})' .format(batch=step+1, size=len(image_files))
                    bar.next()

                if negativeness != []:
                    negativeness_path = os.path.join(output_fp, 'negative.csv')
                    np.savetxt(negativeness_path, np.array(negativeness), delimiter=',')
            bar.finish()
