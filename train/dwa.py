import sys

sys.path.append('..')
import shutil

from dataset.dataloaders import UnetInput
from flerken import pytorchfw
from flerken.models import UNet
from flerken.framework.pytorchframework import set_training, config, ctx_iter, classitems, \
    checkpoint_on_key, assert_workdir
from flerken.framework import train, val
from torch.optim.lr_scheduler import ReduceLROnPlateau
from utils.utils import *
from utils.EarlyStopping import EarlyStopping
from models.wrapper import Wrapper
from tqdm import tqdm
from loss.losses import *
from settings import *


class DWA(pytorchfw):
    def __init__(self, model, rootdir, workname, main_device=0, trackgrad=False):
        super(DWA, self).__init__(model, rootdir, workname, main_device, trackgrad)
        self.audio_dumps_path = os.path.join(DUMPS_FOLDER, 'audio')
        self.visual_dumps_path = os.path.join(DUMPS_FOLDER, 'visuals')
        self.audio_dumps_folder = None
        self.visual_dumps_folder = None
        self.main_device = main_device
        self.grid_unwarp = torch.from_numpy(
            warpgrid(BATCH_SIZE, NFFT // 2 + 1, STFT_WIDTH, warp=False)).to(self.main_device)

        self.set_tensor_scalar_item('l1')
        self.set_tensor_scalar_item('l2')
        if K == 4:
            self.set_tensor_scalar_item('l3')
            self.set_tensor_scalar_item('l4')
        self.set_tensor_scalar_item('loss_tracker')
        self.EarlyStopChecker = EarlyStopping(patience=EARLY_STOPPING_PATIENCE)
        self.val_iterations = 0

    def print_args(self):
        setup_logger('log_info', self.workdir + '/info_file.txt',
                     FORMAT="[%(asctime)-15s %(filename)s:%(lineno)d %(funcName)s] %(message)s]")
        logger = logging.getLogger('log_info')
        self.print_info(logger)
        logger.info(f'\r\t Spectrogram data dir: {ROOT_DIR}\r'
                    'TRAINING PARAMETERS: \r\t'
                    f'Run name: {self.workname}\r\t'
                    f'Batch size {BATCH_SIZE} \r\t'
                    f'Optimizer {OPTIMIZER} \r\t'
                    f'Initializer {INITIALIZER} \r\t'
                    f'Epochs {EPOCHS} \r\t'
                    f'LR General: {LR} \r\t'
                    f'SGD Momentum {MOMENTUM} \r\t'
                    f'Weight Decay {WEIGHT_DECAY} \r\t'
                    f'Pre-trained model:  {PRETRAINED} \r'
                    'MODEL PARAMETERS \r\t'
                    f'Nº instruments (K) {K} \r\t'
                    f'U-Net activation: {ACTIVATION} \r\t'
                    f'U-Net Input channels {INPUT_CHANNELS}\r\t'
                    f'U-Net Batch normalization {USE_BN} \r\t')

    def set_optim(self, *args, **kwargs):
        if OPTIMIZER == 'adam':
            return torch.optim.Adam(*args, **kwargs)
        elif OPTIMIZER == 'SGD':
            return torch.optim.SGD(*args, **kwargs)
        else:
            raise Exception('Non considered optimizer. Implement it')

    def hyperparameters(self):
        self.dataparallel = False
        self.initializer = INITIALIZER
        self.EPOCHS = EPOCHS
        self.DWA_T = DWA_TEMP
        self.optimizer = self.set_optim(self.model.parameters(), momentum=MOMENTUM, lr=LR)
        self.LR = LR
        self.K = K
        self.scheduler = ReduceLROnPlateau(self.optimizer, patience=7, threshold=3e-4)

    def set_config(self):
        self.batch_size = BATCH_SIZE
        self.criterion = IndividualLosses(self.main_device)

    @config
    @set_training
    def train(self):

        self.print_args()
        self.audio_dumps_folder = os.path.join(self.audio_dumps_path, self.workname, 'train')
        create_folder(self.audio_dumps_folder)
        self.visual_dumps_folder = os.path.join(self.visual_dumps_path, self.workname, 'train')
        create_folder(self.visual_dumps_folder)

        training_data = UnetInput('train')
        self.train_loader = torch.utils.data.DataLoader(training_data,
                                                        batch_size=BATCH_SIZE,
                                                        shuffle=True,
                                                        num_workers=10)
        self.train_batches = len(self.train_loader)
        validation_data = UnetInput('val')
        self.val_loader = torch.utils.data.DataLoader(validation_data,
                                                      batch_size=BATCH_SIZE,
                                                      shuffle=True,
                                                      num_workers=10)
        self.val_batches = len(self.val_loader)

        self.avg_cost = np.zeros([self.EPOCHS, self.K], dtype=np.float32)
        self.lambda_weight = np.ones([len(SOURCES_SUBSET), self.EPOCHS])
        for self.epoch in range(self.start_epoch, self.EPOCHS):
            self.cost = list(torch.zeros(self.K))

            # apply Dynamic Weight Average
            if self.epoch == 0 or self.epoch == 1:
                self.lambda_weight[:, self.epoch] = 1.0
            else:
                self.w_1 = self.avg_cost[self.epoch - 1, 0] / self.avg_cost[self.epoch - 2, 0]
                self.w_2 = self.avg_cost[self.epoch - 1, 1] / self.avg_cost[self.epoch - 2, 1]
                self.lambda_weight[0, self.epoch] = \
                    2 * np.exp(self.w_1 / self.DWA_T) / (np.exp(self.w_1 / self.DWA_T) + np.exp(self.w_2 / self.DWA_T))
                self.lambda_weight[1, self.epoch] = \
                    2 * np.exp(self.w_2 / self.DWA_T) / (np.exp(self.w_1 / self.DWA_T) + np.exp(self.w_2 / self.DWA_T))

            with train(self):
                self.run_epoch(self.train_iter_logger)
            self.scheduler.step(self.loss)
            with val(self):
                self.run_epoch()

            self.__update_db__()
            stop = self.EarlyStopChecker.check_improvement(self.loss_tracker_.data.tuple['val'].epoch_array.val,
                                                           self.epoch)
            if stop:
                print('Early Stopping Epoch : [{0}], '
                      'Best Checkpoint Epoch : [{1}]'.format(self.epoch,
                                                             self.EarlyStopChecker.best_epoch))
                break
            print(
                'Epoch: {:04d} | TRAIN: {:.4f} {:.4f}'.format(self.epoch,
                                                              self.avg_cost[self.epoch, 0],
                                                              self.avg_cost[self.epoch, 1]))

    def train_epoch(self, logger):
        j = 0
        self.train_iterations = len(iter(self.train_loader))
        with tqdm(self.train_loader, desc='Epoch: [{0}/{1}]'.format(self.epoch, self.EPOCHS)) as pbar, ctx_iter(self):
            for inputs, visualization in pbar:
                try:
                    self.absolute_iter += 1
                    inputs = self._allocate_tensor(inputs)
                    output = self.model(*inputs) if isinstance(inputs, list) else self.model(inputs)
                    self.component_losses = self.criterion(output)
                    if K == 2:
                        [self.l1, self.l2] = self.component_losses
                        self.loss_tracker = self.l1 + self.l2
                    elif K == 4:
                        [self.l1, self.l2, self.l3, self.l4] = self.component_losses
                        self.loss_tracker = self.l1 + self.l2 + self.l3 + self.l4
                    self.loss = torch.mean(
                        sum(self.lambda_weight[i, self.epoch] * self.component_losses[i] for i in range(self.K)))
                    self.optimizer.zero_grad()
                    self.loss.backward()
                    self.avg_cost[self.epoch] += torch.stack(self.cost).detach().cpu().clone().numpy() / self.train_batches
                    self.gradients()
                    self.optimizer.step()
                    if K == 2:
                        self.cost = [self.l1, self.l2]
                    elif K == 4:
                        self.cost = [self.l1, self.l2, self.l3, self.l4]
                    pbar.set_postfix(loss=self.loss)
                    self.loss_.data.print_logger(self.epoch, j, self.train_iterations, logger)
                    self.tensorboard_writer(self.loss, output, None, self.absolute_iter, visualization)
                    j += 1

                except Exception as e:
                    try:
                        self.save_checkpoint(filename=os.path.join(self.workdir, 'checkpoint_backup.pth'))
                    except:
                        self.err_logger.error('Failed to deal with exception. Couldnt save backup at {0} \n'
                                              .format(os.path.join(self.workdir, 'checkpoint_backup.pth')))
                    self.err_logger.error(str(e))
                    raise e

        for tsi in self.tensor_scalar_items:
            setattr(self, tsi, getattr(self, tsi + '_').data.update_epoch(self.state))
        for idx, src in enumerate(SOURCES_SUBSET):
            self.writer.add_scalars('weights', {'W_' + src: self.lambda_weight[idx, self.epoch].item()}, self.epoch)
        self.tensorboard_writer(self.loss, output, None, self.absolute_iter, visualization)
        self.__update_db__()
        self.save_checkpoint()

    def validate_epoch(self):
        with tqdm(self.val_loader, desc='Validation: [{0}/{1}]'.format(self.epoch, self.EPOCHS)) as pbar, ctx_iter(
                self):
            for inputs, visualization in pbar:
                self.val_iterations += 1
                self.loss_.data.update_timed()
                inputs = self._allocate_tensor(inputs)
                output = self.model(*inputs) if isinstance(inputs, list) else self.model(inputs)
                self.component_losses = self.criterion(output)
                if K == 2:
                    [self.l1, self.l2] = self.component_losses
                    self.loss_tracker = self.l1 + self.l2
                elif K == 4:
                    [self.l1, self.l2, self.l3, self.l4] = self.component_losses
                    self.loss_tracker = self.l1 + self.l2 + self.l3 + self.l4
                self.loss = torch.mean(
                    sum(self.lambda_weight[i, self.epoch] * self.component_losses[i] for i in range(self.K)))
                self.tensorboard_writer(self.loss, output, None, self.absolute_iter, visualization)
                pbar.set_postfix(loss=self.loss)
        for tsi in self.tensor_scalar_items:
            setattr(self, tsi, getattr(self, tsi + '_').data.update_epoch(self.state))
        self.tensorboard_writer(self.loss, output, None, self.absolute_iter, visualization)

    @checkpoint_on_key
    @assert_workdir
    def save_checkpoint(self, filename=None):
        state = {
            'epoch': self.epoch + 1,
            'iter': self.absolute_iter + 1,
            'arch': self.model_version,
            'state_dict': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'loss': self.loss_,
            'key': self.key,
            'scheduler': self.scheduler.state_dict()
        }
        if filename is None:
            filename = os.path.join(self.workdir, self.checkpoint_name)

        elif isinstance(filename, str):
            filename = os.path.join(self.workdir, filename)
        print('Saving checkpoint at : {}'.format(filename))
        torch.save(state, filename)
        if self.loss_tracker_.data.is_best:
            shutil.copyfile(filename, os.path.join(self.workdir, 'best' + self.checkpoint_name))
        print('Checkpoint saved successfully')

    def tensorboard_writer(self, loss, output, gt, absolute_iter, visualization):
        if self.state == 'train':
            iter_val = absolute_iter
        elif self.state == 'val':
            iter_val = self.val_iterations

        if self.iterating:
            if iter_val % PARAMETER_SAVE_FREQUENCY == 0:
                text = visualization[1]
                self.writer.add_text('Filepath', text[-1], iter_val)
                phase = visualization[0].detach().cpu().clone().numpy()
                gt_mags_sq, pred_mags_sq, gt_mags, mix_mag, gt_masks, pred_masks = output
                if len(text) == BATCH_SIZE:
                    grid_unwarp = self.grid_unwarp
                else:  # for the last batch, where the number of samples are generally lesser than the batch_size
                    grid_unwarp = torch.from_numpy(
                        warpgrid(len(text), NFFT // 2 + 1, STFT_WIDTH, warp=False)).to(self.main_device)
                pred_masks_linear = linearize_log_freq_scale(pred_masks, grid_unwarp)
                gt_masks_linear = linearize_log_freq_scale(gt_masks, grid_unwarp)
                oracle_spec = (mix_mag * gt_masks_linear)
                pred_spec = (mix_mag * pred_masks_linear)

                for i, sample in enumerate(text):
                    sample_id = os.path.basename(sample)[:-4]
                    folder_name = os.path.basename(os.path.dirname(sample))
                    pred_audio_out_folder = os.path.join(self.audio_dumps_folder, folder_name, sample_id)
                    create_folder(pred_audio_out_folder)
                    visuals_out_folder = os.path.join(self.visual_dumps_folder, folder_name, sample_id)
                    create_folder(visuals_out_folder)

                    for j, source in enumerate(SOURCES_SUBSET):
                        gt_audio = torch.from_numpy(
                            istft_reconstruction(gt_mags.detach().cpu().numpy()[i][j], phase[i][0], HOP_LENGTH))
                        pred_audio = torch.from_numpy(
                            istft_reconstruction(pred_spec.detach().cpu().numpy()[i][j], phase[i][0], HOP_LENGTH))
                        librosa.output.write_wav(os.path.join(pred_audio_out_folder, 'GT_' + source + '.wav'),
                                                 gt_audio.cpu().detach().numpy(), TARGET_SAMPLING_RATE)
                        librosa.output.write_wav(os.path.join(pred_audio_out_folder, 'PR_' + source + '.wav'),
                                                 pred_audio.cpu().detach().numpy(), TARGET_SAMPLING_RATE)

                        ### PLOTTING MAG SPECTROGRAMS ###
                        save_spectrogram(gt_mags[i][j].unsqueeze(0).detach().cpu(),
                                         os.path.join(visuals_out_folder, source), '_MAG_GT.png')
                        save_spectrogram(oracle_spec[i][j].unsqueeze(0).detach().cpu(),
                                         os.path.join(visuals_out_folder, source), '_MAG_ORACLE.png')
                        save_spectrogram(pred_spec[i][j].unsqueeze(0).detach().cpu(),
                                         os.path.join(visuals_out_folder, source), '_MAG_ESTIMATE.png')

                ### PLOTTING MAG SPECTROGRAMS ###
                plot_spectrogram(self.writer, gt_mags.detach().cpu().view(-1, 1, 512, 256)[:8],
                                 self.state + '_GT_MAG', iter_val)
                plot_spectrogram(self.writer, (pred_masks_linear * mix_mag).detach().cpu().view(-1, 1, 512, 256)[:8],
                                 self.state + '_PRED_MAG', iter_val)

        else:
            if K == 2:
                self.writer.add_scalars(self.state + ' losses_epoch', {'Voice Est Loss': self.l1}, self.epoch)
                self.writer.add_scalars(self.state + ' losses_epoch', {'Acc Est Loss': self.l2}, self.epoch)
            elif K == 4:
                self.writer.add_scalars(self.state + ' losses_epoch', {'Voice Est Loss': self.l1}, self.epoch)
                self.writer.add_scalars(self.state + ' losses_epoch', {'Drums Est Loss': self.l2}, self.epoch)
                self.writer.add_scalars(self.state + ' losses_epoch', {'Bass Est Loss': self.l3}, self.epoch)
                self.writer.add_scalars(self.state + ' losses_epoch', {'Other Est Loss': self.l4}, self.epoch)


def main():
    os.environ['CUDA_VISIBLE_DEVICES'] = '0,1'

    # SET MODEL
    u_net = UNet([32, 64, 128, 256, 512, 1024, 2048], K, None, verbose=False, useBN=True, dropout=DROPOUT)
    model = Wrapper(u_net, main_device=MAIN_DEVICE)

    if not os.path.exists(ROOT_DIR):
        raise Exception('Directory does not exist')
    work = DWA(model, ROOT_DIR, PRETRAINED, trackgrad=TRACKGRAD)
    work.model_version = 'DWA'
    work.train()


if __name__ == '__main__':
    main()

# Usage python3 dwa.py --train/test
