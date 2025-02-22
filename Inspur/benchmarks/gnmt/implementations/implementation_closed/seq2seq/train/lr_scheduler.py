import logging
import math

import torch
from mlperf_logging.mllog import constants

from seq2seq.utils import log_event


def perhaps_convert_float(param, total):
    if isinstance(param, float):
        param = int(param * total)
    return param


class WarmupMultiStepLR(torch.optim.lr_scheduler._LRScheduler):
    """
    Learning rate scheduler with exponential warmup and step decay.
    """
    def __init__(self, optimizer, iterations, warmup_steps=0,
                 remain_steps=1.0, decay_interval=None, decay_steps=4,
                 decay_factor=0.5, last_epoch=-1):
        """
        Constructor of WarmupMultiStepLR.

        Parameters: warmup_steps, remain_steps and decay_interval accept both
        integers and floats as an input. Integer input is interpreted as
        absolute index of iteration, float input is interpreted as a fraction
        of total training iterations (epochs * steps_per_epoch).

        If decay_interval is None then the decay will happen at regulary spaced
        intervals ('decay_steps' decays between iteration indices
        'remain_steps' and 'iterations').

        :param optimizer: instance of optimizer
        :param iterations: total number of training iterations
        :param warmup_steps: number of warmup iterations
        :param remain_steps: start decay at 'remain_steps' iteration
        :param decay_interval: interval between LR decay steps
        :param decay_steps: max number of decay steps
        :param decay_factor: decay factor
        :param last_epoch: the index of last iteration
        """

        # iterations before learning rate reaches base LR
        self.warmup_steps = perhaps_convert_float(warmup_steps, iterations)
        logging.info(f'Scheduler warmup steps: {self.warmup_steps}')

        # iteration at which decay starts
        self.remain_steps = perhaps_convert_float(remain_steps, iterations)
        logging.info(f'Scheduler remain steps: {self.remain_steps}')

        # number of steps between each decay
        if decay_interval is None:
            # decay at regulary spaced intervals
            decay_iterations = iterations - self.remain_steps
            self.decay_interval = decay_iterations // (decay_steps)
            self.decay_interval = max(self.decay_interval, 1)
        else:
            self.decay_interval = perhaps_convert_float(decay_interval,
                                                        iterations)
        logging.info(f'Scheduler decay interval: {self.decay_interval}')

        # multiplicative decay factor
        self.decay_factor = decay_factor
        logging.info(f'Scheduler decay factor: {self.decay_factor}')

        # max number of decay steps
        self.decay_steps = decay_steps
        logging.info(f'Scheduler max decay steps: {self.decay_steps}')

        if self.warmup_steps > self.remain_steps:
            logging.warn(f'warmup_steps should not be larger than '
                         f'remain_steps, setting warmup_steps=remain_steps')
            self.warmup_steps = self.remain_steps

        log_event(key=constants.OPT_LR_ALT_DECAY_FUNC,
                  value=True)
        log_event(key=constants.OPT_LR_ALT_WARMUP_FUNC,
                  value=True)

        log_event(key=constants.OPT_LR_DECAY_INTERVAL,
                  value=self.decay_interval)
        log_event(key=constants.OPT_LR_DECAY_FACTOR,
                  value=self.decay_factor)
        log_event(key=constants.OPT_LR_DECAY_STEPS,
                  value=self.decay_steps)
        log_event(key=constants.OPT_LR_REMAIN_STEPS,
                  value=self.remain_steps)
        log_event(key=constants.OPT_LR_WARMUP_STEPS,
                  value=self.warmup_steps)

        super(WarmupMultiStepLR, self).__init__(optimizer, last_epoch)

    def get_lr(self):
        if self.last_epoch <= self.warmup_steps:
            # exponential lr warmup
            if self.warmup_steps != 0:
                warmup_factor = math.exp(math.log(0.01) / self.warmup_steps)
            else:
                warmup_factor = 1.0
            inv_decay = warmup_factor ** (self.warmup_steps - self.last_epoch)
            lr = [base_lr * inv_decay for base_lr in self.base_lrs]

        elif self.last_epoch >= self.remain_steps:
            # step decay
            decay_iter = self.last_epoch - self.remain_steps
            num_decay_steps = decay_iter // self.decay_interval + 1
            num_decay_steps = min(num_decay_steps, self.decay_steps)
            lr = [
                base_lr * (self.decay_factor ** num_decay_steps)
                for base_lr in self.base_lrs
                ]
        else:
            # base lr
            lr = [base_lr for base_lr in self.base_lrs]
        return lr
