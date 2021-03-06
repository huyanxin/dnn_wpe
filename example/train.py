#!/usr/bin/env python
import json
import logging
import os
import random
import time
from collections import OrderedDict
from concurrent.futures.thread import ThreadPoolExecutor
from pathlib import Path
from typing import List, Union, Sequence, Optional, Dict

import humanfriendly
import numpy
from pystoi.stoi import stoi
import sacred
from sacred.observers import FileStorageObserver
import torch
import torch_complex.functional as FC
from torch.utils.data import DataLoader
from torch_complex.tensor import ComplexTensor
from typeguard import check_argument_types

from pytorch_wpe import wpe

# From local directory
from data_parallel import MyDataParallel
from dataset import \
    (WavRIRNoiseDataset, DescendingOrderedBatchSampler,
     collate_fn, ScpScpDataset, Chunk)
from model import DNN_WPE
from utils import get_commandline_args, Stft, calc_pesq


def get_logger(
        name='',
        format_str=
        '%(asctime)s\t[%(levelname)s]%(module)s:%(lineno)s\t%(message)s',
        date_format="%Y-%m-%d %H:%M:%S"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter(fmt=format_str, datefmt=date_format)
    handler.setFormatter(formatter)

    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger


global_logger = get_logger(__file__)


class Reporter:
    def __init__(self, logger: logging.Logger=global_logger):
        self.stats = OrderedDict()
        self.logger = logger

    def __setitem__(self, key, value):
        self.stats.setdefault(key, []).append(value)

    def report(self, prefix: str='', nhistory: int=0):
        message = []
        for key, value in self.stats.items():
            mean = self.value(key, nhistory=nhistory)
            message.append(f'{key}={mean}')
        self.logger.info(f'{prefix}' + ' '.join(message))

    def value(self, key, nhistory: int=0):
        value = self.stats[key]
        mean = numpy.ma.masked_invalid(value[-nhistory:]).mean()
        # If there are no valid elements:
        if mean == 'masked':
            mean = 'nan'
        return mean


def mse_loss(input, target):
    """Normalized Mean Squared Error"""
    assert check_argument_types()
    assert input.shape == target.shape, (input.shape, target.shape)

    loss = torch.norm(input - target) ** 2 / torch.norm(target) ** 2
    # loss = torch.nn.functional.mse_loss(input,  target)
    return loss


def unpad(x: torch.Tensor, ilens: torch.LongTensor,
          length_dim: int=1) -> List[torch.Tensor]:
    # x: (B, ..., T, ...)
    if length_dim < 0:
        length_dim = x.dim() + length_dim

    xs = []
    for _x, l in zip(x, ilens):
        # ind = (:, ..., :l, , :)
        ind = tuple(slice(0, l) if i == length_dim else slice(None)
                    for i in range(1, x.dim()))
        # _x: (T, ...) -> (l, ...)
        xs.append(_x[ind])
    # B x (..., li, ...)
    return xs


class LossCalculator(torch.nn.Module):
    def __init__(self, model: torch.nn.Module, stft_conf: str = None,
                 lcontext: int=4,
                 rcontext: int=4,
                 pesq_nworker: int = 1):
        super().__init__()
        self.model = model
        self.stft_func = Stft(stft_conf)
        self.pesq_nworker = pesq_nworker
        self.lcontext = lcontext
        self.rcontext = rcontext

    def forward(self, xs: ComplexTensor, ts: ComplexTensor,
                ilens: torch.LongTensor,
                loss_types: Union[str, Sequence[str]]='power_mse',
                ref_channel: int=0) -> Dict[str, torch.Tensor]:
        # xs: (B, C, T, F), ts: (B, T, F)
        if isinstance(loss_types, str):
            loss_types = [loss_types]

        # ys: (B, C, T, F), power: (B, C, T, F)
        for loss_type in loss_types:
            if 'dnnwpe' in loss_type:
                return_wpe = True
                break
        else:
            return_wpe = False
        ys, power = self.model(xs, ilens, return_wpe=return_wpe)

        r = -self.rcontext if self.rcontext != 0 else None
        xs = xs[:, :, self.lcontext:r, :]

        if ys is not None:
            assert xs.shape == ys.shape, (xs.shape, ys.shape)
        assert xs.shape == power.shape, (xs.shape, power.shape)

        uts = None
        uys = None
        upower = None
        ys_time = None
        ts_time = None
        xs_time = None

        loss_dict = OrderedDict()
        for loss_type in loss_types:
            if loss_type == 'dnnwpe_power_mse':
                if uys is None:
                    uys = FC.cat(unpad(ys, ilens, length_dim=2), dim=1)
                if uts is None:
                    uts = FC.cat(unpad(ts, ilens, length_dim=2), dim=1)
                    
                _ys = uys.real ** 2 + uys.imag ** 2
                _ts = uts.real ** 2 + uts.imag ** 2

                _ys = _ys.log()
                _ts = _ts.log()
                loss = mse_loss(_ys, _ts)
                
            elif loss_type == 'dnnwpe_mse':
                if uys is None:
                    uys = FC.cat(unpad(ys, ilens, length_dim=2), dim=1)
                if uts is None:
                    uts = FC.cat(unpad(ts, ilens, length_dim=2), dim=1)
                    
                _ys = torch.cat([uys.real, uys.imag], dim=-1)
                _ts = torch.cat([uts.real, uts.imag], dim=-1)
                loss = mse_loss(_ys, _ts)

            elif loss_type == 'power_mse':
                if upower is None:
                    upower = torch.cat(unpad(power, ilens, length_dim=2), dim=1)
                if uts is None:
                    uts = FC.cat(unpad(ts, ilens, length_dim=2), dim=1)
                    
                _ts = uts.real ** 2 + uts.imag ** 2
                _upower = upower
                _ts = _ts

                loss = mse_loss(_upower, _ts)

            # For evaluation as not differentiable
            elif loss_type == 'dnnwpe_stoi':

                # Use the first channel only to make faster calculation
                if ys_time is None:
                    # _ys: List[torch.Tensor]: B x [C, T, F]
                    _ys = unpad(ys, ilens, length_dim=2)
                    # ys_time: List[np.ndarray]: B x [T]
                    ys_time = [self.stft_func.istft(_y[0].cpu().numpy().T)
                               for _y in _ys]
                if ts_time is None:
                    # _ts: List[torch.Tensor]: B x [C, T, F]
                    _ts = unpad(ts, ilens, length_dim=2)
                    # ts_time: List[np.ndarray]: B x [T]
                    ts_time = [self.stft_func.istft(_t[0].cpu().numpy().T)
                               for _t in _ts]

                _losses = []

                for _y, _t in zip(ys_time, ts_time):
                    # Single channel only
                    _losses.append(stoi(_t, _y, self.stft_func.fs))
                loss = torch.tensor(numpy.mean(_losses))

            # For evaluation as not differentiable
            elif loss_type == 'dnnwpe_pesq':

                # Use the first channel only to make faster calculation
                if ys_time is None:
                    # _ys: List[torch.Tensor]: B x [C, T, F]
                    _ys = unpad(ys, ilens, length_dim=2)
                    # ys_time: List[np.ndarray]: B x [T]
                    ys_time = [self.stft_func.istft(_y[0].cpu().numpy().T)
                               for _y in _ys]
                if ts_time is None:
                    # _ts: List[torch.Tensor]: B x [C, T, F]
                    _ts = unpad(ts, ilens, length_dim=2)
                    # ts_time: List[np.ndarray]: B x [T]
                    ts_time = [self.stft_func.istft(_t[0].cpu().numpy().T)
                               for _t in _ts]

                _fns = []
                # PESQ via subprocess can be parallerize by threading
                e = ThreadPoolExecutor(self.pesq_nworker)
                for _y, _t in zip(ys_time, ts_time):
                    _y *= numpy.iinfo(numpy.int16).max - 1
                    _y = _y.astype(numpy.int16)

                    _t *= numpy.iinfo(numpy.int16).max - 1
                    _t = _t.astype(numpy.int16)
                    fn = e.submit(calc_pesq, _t, _y, self.stft_func.fs)
                    _fns.append(fn)

                _losses = []
                for fn in _fns:
                    v = fn.result()
                    _losses.append(v)

                loss = torch.tensor(numpy.mean(_losses))

            # For evaluation as not differentiable
            elif loss_type == 'unprocessed_pesq':
                # Use the first channel only to make faster calculation
                if xs_time is None:
                    # _ys: List[torch.Tensor]: B x [C, T, F]
                    _xs = unpad(xs, ilens, length_dim=2)
                    # ys_time: List[np.ndarray]: B x [T]
                    xs_time = [self.stft_func.istft(_x[0].cpu().numpy().T)
                               for _x in _xs]
                if ts_time is None:
                    # _ts: List[torch.Tensor]: B x [C, T, F]
                    _ts = unpad(ts, ilens, length_dim=2)
                    # ts_time: List[np.ndarray]: B x [T]
                    ts_time = [self.stft_func.istft(_t[0].cpu().numpy().T)
                               for _t in _ts]

                _fns = []

                # PESQ via subprocess can be parallerize by threading
                e = ThreadPoolExecutor(self.pesq_nworker)
                for _x, _t in zip(xs_time, ts_time):
                    _x = _x * numpy.iinfo(numpy.int16).max - 1
                    _x = _x.astype(numpy.int16)

                    _t = _t * numpy.iinfo(numpy.int16).max - 1
                    _t = _t.astype(numpy.int16)
                    fn = e.submit(calc_pesq, _t, _x, self.stft_func.fs)
                    _fns.append(fn)

                _losses = []
                for fn in _fns:
                    v = fn.result()
                    _losses.append(v)

                loss = torch.tensor(numpy.mean(_losses))

            elif loss_type == 'wpe_pesq':
                with torch.no_grad():
                    # (B, C, T, F) -> (B, F, C, T)
                    _xs = xs.permute(0, 3, 1, 2).contiguous()
                    # _ys: (B, F, C, T)
                    _ys = wpe(_xs, 5, 3, 3)[:, :, ref_channel]
                    _ys = unpad(_ys, ilens, length_dim=2)
                    ys_time = [self.stft_func.istft(_y.cpu().numpy())
                               for _y in _ys]

                if ts_time is None:
                    # _ts: List[torch.Tensor]: B x [C, T, F]
                    _ts = unpad(ts, ilens, length_dim=2)
                    # ts_time: List[np.ndarray]: B x [T]
                    ts_time = [self.stft_func.istft(_t[0].cpu().numpy().T)
                               for _t in _ts]

                _fns = []

                # PESQ via subprocess can be parallerize by threading
                e = ThreadPoolExecutor(self.pesq_nworker)
                for _y, _t in zip(ys_time, ts_time):
                    _y *= numpy.iinfo(numpy.int16).max - 1
                    _y = _y.astype(numpy.int16)

                    _t *= numpy.iinfo(numpy.int16).max - 1
                    _t = _t.astype(numpy.int16)
                    fn = e.submit(calc_pesq, _t, _y, self.stft_func.fs)
                    _fns.append(fn)

                _losses = []
                for fn in _fns:
                    v = fn.result()
                    _losses.append(v)

                loss = torch.tensor(numpy.mean(_losses))

            elif loss_type == 'wpe_mse':
                # Note: No updated parameters existing
                # 96328786.2853478
                if uts is None:
                    uts = FC.cat(unpad(ts, ilens, length_dim=2), dim=1)
                with torch.no_grad():
                    # (B, C, T, F) -> (B, F, C, T)
                    _xs = xs.permute(0, 3, 1, 2)
                    # _ys: (B, F, C, T) -> (B, T, F)
                    _ys = wpe(_xs, 5, 3, 3)[:, :, ref_channel].transpose(1, 2)
                    _uys = FC.cat(unpad(_ys, ilens, length_dim=1), dim=0)
                    _ys = _uys.real ** 2 + _uys.imag ** 2
                    _ts = uts.real ** 2 + uts.imag ** 2
                    _ts = _ts[ref_channel]

                    loss = mse_loss(_ys, _ts)

            else:
                raise NotImplementedError(f'loss_type={loss_type}')

            # Don't return scalar
            loss_dict[loss_type] = loss[None]

        return loss_dict


def check_gradient(model: torch.nn.Module) -> bool:
    for param in model.parameters():
        if not torch.all(torch.isfinite(param.grad)):
            return False
    return True


def train(loss_calculator: torch.nn.Module,
          data_loader,
          optimizer: torch.optim.Optimizer,
          epoch: int,
          report_interval: int=1000,
          loss_types: Union[str, Sequence[str]]='mse',
          ref_channel: int=0,
          loss_weight: Sequence[float]=1.0,
          grad_clip: float=None):
    assert check_argument_types()
    if isinstance(loss_types, str):
        loss_types = [loss_types]
    if isinstance(loss_weight, float):
        loss_weight = \
            [loss_weight] + [0 for _ in range(len(loss_types) - 1)]
    if len(loss_types) != len(loss_weight):
        raise RuntimeError(
            f'Mismatch: {len(loss_types)} != {len(loss_weight)}')

    reporter = Reporter()

    loss_calculator.train()
    miss_count = 0
    for ibatch, (_, xs, ts, ilens) in enumerate(data_loader):
        # xs: (B, C, T, F), ts: (B, C, T, F), ilens: (B,)

        optimizer.zero_grad()
        try:
            loss_dict = loss_calculator(xs, ts, ilens,
                                        loss_types=loss_types,
                                        ref_channel=ref_channel)
        except RuntimeError as e:
            # If inverse() failed in wpe
            if str(e).startswith('inverse_cuda: For batch'):
                global_logger.warning('Skipping this step. ' + str(e))
                miss_count += 1
                continue
            raise

        sloss = 0
        for iloss, (loss_type, loss) in enumerate(loss_dict.items()):
            # Averaging between each gpu devices
            loss = loss.mean()
            reporter[loss_type] = loss.item()

            if loss_weight[iloss] != 0:
                sloss += loss_weight[iloss] * loss
            else:
                sloss += loss
        sloss.backward()

        if grad_clip is not None:
            torch.nn.utils.clip_grad_norm_(loss_calculator.parameters(),
                                           grad_clip)

        if check_gradient(loss_calculator):
            optimizer.step()
        else:
            global_logger.warning('The gradient is diverged. Skip updating.')

        if (ibatch + 1) % report_interval == 0:
            reporter.report(f'Train {epoch}epoch {ibatch + 1}: ',
                            nhistory=report_interval - miss_count)
            miss_count = 0


def test(loss_calculator: torch.nn.Module,
         data_loader,
         epoch: int,
         loss_types: Union[str, Sequence[str]]='mse',
         ref_channel: int=0):
    assert check_argument_types()
    reporter = Reporter()

    loss_calculator.eval()
    for _, xs, ts, ilens in data_loader:
        with torch.no_grad():
            loss_dict = loss_calculator(xs, ts, ilens,
                                        loss_types=loss_types,
                                        ref_channel=ref_channel)
        for loss_type, loss in loss_dict.items():
            # Averaging between each gpu devices
            loss = loss.mean()
            reporter[loss_type] = loss.item()
    reporter.report(f'Eval {epoch}epoch: : ')

    return reporter


ex = sacred.Experiment('train')
ex.logger = global_logger


@ex.config
def config():
    """Configuration for training

    This script depends on sacred for CLI system.
    You can change the configuration: e.g.

        % python train.py with batch_size=32 workdir=my_exp
        % python train.py with opt_config.lr=0.1
    """
    # Output directory
    workdir = 'exp'
    model_file = None
    optimizer_file = None
    start_epoch = 1

    # Input files
    train_scp = 'data/train/wav_rir_noise.scp'
    train_nframes = 'data/train/utt2nframes'

    test_scp = 'data/dev/wav_rir_noise.scp'

    stft_conf = './stft.json'

    nworker = 4

    seed = 0
    # None indicates using all visible devices
    ngpu = None
    batch_size = 16
    nepoch = 30
    report_interval = 100
    resume = None

    # 'SGD', 'Adam'
    opt_type = 'SGD'
    opt_config = {'lr': 0.2,
                  # 'weight_decay': 0.0001,
                  # 'momentum': 0.9
                  }

    loss_type = ['power_mse']
    eval_type = ['power_mse', 'dnnwpe_mse']
    loss_weight = [1.]
    grad_clip = 5.
    # The reference channel used for loss calculation
    ref_channel = 0

    model_config = {'feat_type': 'log_power',
                    'out_type': 'mask',
                    'model_type': 'lstm',
                    'num_layers': 2,
                    'hidden_size': 300,
                    }
    delay = 0
    uttlevel = True
    width = 100
    lcontext = 0
    rcontext = 0
    if uttlevel:
        width = None
        lcontext = 0
        rcontext = 0

    norm_scale = True

    Path(workdir).mkdir(parents=True, exist_ok=True)
    observer = FileStorageObserver.create(str(Path(workdir) / 'config'))
    ex.observers.append(observer)
    del observer


@ex.automain
def main(seed: int,
         ngpu: Optional[int],
         batch_size: int,
         workdir: str,
         train_scp: str,
         train_nframes: str,
         test_scp: str,
         stft_conf: str,
         opt_type: str,
         opt_config: dict,
         report_interval: int,
         nepoch: int,
         loss_type: Union[str, Sequence[str]],
         eval_type: Sequence[str],
         loss_weight: Union[float, Sequence[float]],
         ref_channel: int,
         nworker: int,
         grad_clip: float,
         model_config: dict,
         uttlevel: bool,
         width: Optional[int],
         lcontext: int,
         rcontext: int,
         model_file: Optional[str],
         optimizer_file: Optional[str],
         delay: int = 0,
         norm_scale: bool = True,
         start_epoch: int = 1,
         ):
    global_logger.info(get_commandline_args())
    assert check_argument_types()
    random.seed(seed)
    torch.manual_seed(seed)
    numpy.random.seed(seed)

    if ngpu is None:
        ngpu = torch.cuda.device_count()
    global_logger.info(f'NGPU: {ngpu}, NWORKER={nworker}, '
                       f'HOST={os.uname()[1]}')

    if uttlevel:
        _batch_size = batch_size
    else:
        _batch_size = 1

    train_loader = DataLoader(
        dataset=WavRIRNoiseDataset(train_scp, stft_conf=stft_conf,
                                   delay=delay, norm_scale=norm_scale),
        batch_sampler=DescendingOrderedBatchSampler(batch_size=_batch_size,
                                                    shuffle=True,
                                                    nframes=train_nframes),
        num_workers=nworker,
        collate_fn=collate_fn)

    test_loader = DataLoader(
        dataset=WavRIRNoiseDataset(test_scp, stft_conf=stft_conf,
                                   delay=delay, norm_scale=norm_scale),
        batch_size=1,
        num_workers=nworker,
        collate_fn=collate_fn)

    if not uttlevel:
        train_loader = Chunk(train_loader, batch_size=batch_size,
                             width=width, rcontext=rcontext, lcontext=lcontext)

    # MyDataParallel can handle "ComplexTensor"
    input_size = json.load(open(stft_conf))['nfft'] // 2 + 1

    model_config.update(input_size=input_size * (1 + lcontext + rcontext),
                        out_size=input_size,
                        rcontext=rcontext,
                        lcontext=lcontext)

    model = DNN_WPE(**model_config)

    with open(f'{workdir}/model_args.json', 'w') as f:
        model_config.update(width=width, norm_scale=norm_scale)
        json.dump(model_config, f, indent=4)

    if model_file is not None:
        model.load_state_dict(torch.load(model_file))

    loss_calculator = MyDataParallel(
        LossCalculator(model, stft_conf=stft_conf, pesq_nworker=nworker,
                       rcontext=rcontext, lcontext=lcontext,
                       ),
        device_ids=list(range(ngpu)))

    OptimzerClass = getattr(torch.optim, opt_type)
    optimizer = OptimzerClass(model.parameters(), **opt_config)
    if optimizer_file is not None:
        optimizer.load_state_dict(torch.load(optimizer_file))

    global_logger.info(model)
    global_logger.info(optimizer)

    initial_lr = 0.
    for param_group in optimizer.param_groups:
        initial_lr = param_group['lr']

    elapsed = {}
    if True:
        prev_reporter = None
    else:
        prev_reporter = test(
            loss_calculator, test_loader, epoch=0,
            loss_types=['dnnwpe_mse'],
            ref_channel=ref_channel)
    for epoch in range(start_epoch, nepoch + 1):
        global_logger.info(f'Start {epoch}/{nepoch} epoch')
        t = time.perf_counter()

        train(loss_calculator, train_loader, optimizer,
              epoch=epoch, report_interval=report_interval,
              loss_types=loss_type,
              loss_weight=loss_weight,
              ref_channel=ref_channel,
              grad_clip=grad_clip)

        reporter = test(
            loss_calculator, test_loader, epoch=epoch,
            loss_types=eval_type,
            ref_channel=ref_channel)

        if prev_reporter is not None:
            if isinstance(loss_type, str):
                _loss_type = loss_type
            else:
                _loss_type = loss_type[0]
            diff = reporter.value(_loss_type) - prev_reporter.value(_loss_type)
            if diff > 0.:
                global_logger.info('Not improved, Reduce lr')
                for param_group in optimizer.param_groups:
                    param_group['lr'] *= 0.5

        prev_reporter = reporter

        torch.save(model.state_dict(), f'{workdir}/model_{epoch}.pt')
        torch.save(optimizer.state_dict(), f'{workdir}/optimizer_{epoch}.pt')

        elapsed[epoch] = time.perf_counter() - t
        this = humanfriendly.format_timespan(elapsed[epoch])
        mean = numpy.mean(list(elapsed.values()))
        expect = humanfriendly.format_timespan(mean * (nepoch - epoch))
        global_logger.info(f'Elapsed time for {epoch}epoch: {this}, '
                           f'Expected remaining time: {expect}')

        for param_group in optimizer.param_groups:
            if param_group['lr'] < initial_lr / 2 ** 15:
                global_logger.info('Threshold!')
                return
