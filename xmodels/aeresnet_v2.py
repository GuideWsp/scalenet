# -*- coding: utf-8 -*-
__author__ = 'ooo'
__date__ = '2019/1/9 12:17'

"""
  WaveResNet => + DoubleCouple + SingleCouple + Summary + Boost, summary中无merge， 无BottleNeck
"""
import math, torch
import torch.nn as nn
import torch.nn.functional as F
from xmodules.downsample import DownSampleA, DownSampleB, DownSampleC, DownSampleD, DownSampleE, DownSampelH
from xmodules.rockblock import RockBlock, RockBlockA, RockBlockB, RockBlockC, RockBlockD, \
                      RockBlockE, RockBlockF, RockBlockH, RockBlockM
from xmodules.classifier import ViewLayer, AdaAvgPool, Activate, XClassifier


class DoubleCouple(nn.Module):
    exp1 = 1  # exp1, exp2 is the internal expand of this block, set it >=1 will be ok.
    exp2 = 1
    exp3 = 2  # must==2, double the channel for the next down-size layer
    exp4 = 1  # maybe==1/2, have or no have channel expand for the last fc layer? --> self.last_expand

    down_func = {'A': DownSampleA, 'B': DownSampleB, 'C': DownSampleC,
                 'D': DownSampleD, 'E': DownSampleE, 'H': DownSampelH}

    # A >> B >>> C , exp:22>>26>>>25, A extra Parameters

    def __init__(self, depth, after=True, down=False, last_down=False, last_expand=1, last_branch=1,
                 dfunc='A', classify=0, nclass=1000, blockexp=(1, 1), slink='A'):
        super(DoubleCouple, self).__init__()
        assert last_branch <= 3, '<last_branch> of DoubleCouple should be <= 3...'
        self.depth = depth
        self.after = after  # dose still have another DoubleCouple behind of this DoubleCouple. ?
        self.down = down  # dose connect to a same-size DoubleCouple or down-sized DoubleCouple. ?
        self.last_branch = last_branch  # how many output-ways (branches) for the classifier ?
        self.last_down = last_down  # dose down size for the classifier?
        self.last_expand = last_expand  # dose expand channel for the classifier

        self.slink = slink  # A >> C > B
        self.dfunc = dfunc
        self.down_func = self.down_func[dfunc]
        self.classify = classify
        self.exp1, self.exp2 = blockexp

        self.active_fc = False
        self.bn1 = nn.BatchNorm2d(depth)
        self.conv1 = nn.Conv2d(depth, depth * self.exp1, 3, stride=2, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(depth * self.exp1)
        self.conv2 = nn.Conv2d(depth * self.exp1, depth * self.exp2, 3, stride=2, padding=1, bias=False)
        self.bn3 = nn.BatchNorm2d(depth * self.exp2)
        self.deconv3 = nn.ConvTranspose2d(depth * self.exp2, depth * self.exp1, 4, stride=2, padding=1, bias=False,
                                          output_padding=0, dilation=1)
        self.bn4 = nn.BatchNorm2d(depth * self.exp1)
        self.deconv4 = nn.ConvTranspose2d(depth * self.exp1, depth, 4, stride=2, padding=1, bias=False,
                                          output_padding=0, dilation=1)

        if self.classify > 0:
            self.classifier = XClassifier(depth * self.exp2, nclass)

        if self.after:
            if self.down:
                self.down_res4 = self.down_func(depth, depth * self.exp3)
                self.down_res3 = self.down_func(depth * self.exp1, depth * self.exp1 * self.exp3)
                self.down_res2 = self.down_func(depth * self.exp2, depth * self.exp2 * self.exp3)
        else:
            if self.last_down:
                if self.last_branch >= 1:
                    self.down_last4 = self.down_func(depth, depth * self.last_expand)
                if self.last_branch >= 2:
                    self.down_last3 = self.down_func(depth * self.exp1, depth * self.last_expand)
                if self.last_branch >= 3:
                    self.down_last2 = self.down_func(depth * self.exp2, depth * self.last_expand)
            else:
                if self.classify > 0 and self.last_branch == 3:
                    # 最后一个Couple的中间层被当做branch输出而对接在Summary上.
                    # 因此，删除此Couple自带的Classifier,以免与Summary中的Classifier重复.
                    delattr(self, 'classifier')
                    self.classify = 0
                    print('Note: 1 xfc will be deleted because of duplication!')

    def forward(self, x):
        if isinstance(x, tuple):
            x1, x2, x3, pred = x
            # print('x1, x2, x3: ', x1.size(), x2.size(), x3.size(), pred)
        else:
            x1, x2, x3, pred = x, None, None, None

        # add-style
        if self.slink == 'A':
            res1 = self.conv1(F.relu(self.bn1(x1)))
            out = res1 if x2 is None else res1 + x2
            res2 = self.conv2(F.relu(self.bn2(out)))
            out = res2 if x3 is None else res2 + x3
            res3 = self.deconv3(F.relu(self.bn3(out)))
            res4 = self.deconv4(F.relu(self.bn4(res3 + res1)))
            res4 = res4 + x1

        elif self.slink == 'X':
            # first add, then shortcut, low < 'A'
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res1 = res1 if x2 is None else res1 + x2
            res2 = self.conv2(F.relu(self.bn2(res1)))
            res2 = res2 if x3 is None else res2 + x3
            out = res2
            res3 = self.deconv3(F.relu(self.bn3(res2)))
            res3 = res3 + res1
            res4 = self.deconv4(F.relu(self.bn4(res3)))
            res4 = res4 + x1

        elif self.slink == 'B':
            # A的简化版，只有每个block内部的2个内连接
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.conv2(F.relu(self.bn2(res1)))
            res3 = self.deconv3(F.relu(self.bn3(res2)))
            res4 = self.deconv4(F.relu(self.bn4(res3 + res1)))
            res4 = res4 + x1
            out = res2

        elif self.slink == 'C':
            # A的简化版，只有每个block内内部最大尺寸的那1个内连接， 类似resnet
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.conv2(F.relu(self.bn2(res1)))
            res3 = self.deconv3(F.relu(self.bn3(res2)))
            res4 = self.deconv4(F.relu(self.bn4(res3)))
            res4 = res4 + x1
            out = res2

        elif self.slink == 'D':
            # A的简化版，2个夸block连接，1个block内连接(res4+x1)
            res1 = self.conv1(F.relu(self.bn1(x1)))
            out = res1 if x2 is None else res1 + x2
            res2 = self.conv2(F.relu(self.bn2(out)))
            out = res2 if x3 is None else res2 + x3
            res3 = self.deconv3(F.relu(self.bn3(out)))
            res4 = self.deconv4(F.relu(self.bn4(res3)))
            res4 = res4 + x1

        elif self.slink == 'E':
            # A的简化版，只有2个夸block连接
            res1 = self.conv1(F.relu(self.bn1(x1)))
            out = res1 if x2 is None else res1 + x2
            res2 = self.conv2(F.relu(self.bn2(out)))
            out = res2 if x3 is None else res2 + x3
            res3 = self.deconv3(F.relu(self.bn3(out)))
            res4 = self.deconv4(F.relu(self.bn4(res3)))
            res4 = res4

        elif self.slink == 'F':
            # A的简化版，1个夸block连接，1个block内连接
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.conv2(F.relu(self.bn2(res1)))
            out = res2 if x3 is None else res2 + x3
            res3 = self.deconv3(F.relu(self.bn3(out)))
            res4 = self.deconv4(F.relu(self.bn4(res3)))
            res4 = res4 + x1

        # cat-style
        elif self.slink == 'G':
            # Note: x2, x3 在当前block内不生效，全部累加到本stage的最后一个block内, 在downsize的时候生效
            # only x1 for calculate, x2 / x3 all moved to next block until the last block
            # Not good! x2, x3 be wasted .
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.conv2(F.relu(self.bn2(res1)))
            res3 = self.deconv3(F.relu(self.bn3(res2)))
            res4 = self.deconv4(F.relu(self.bn4(res3)))
            out = res2

            res1 = res1 if x2 is None else res1 + x2
            res2 = res2 if x3 is None else res2 + x3
            res3 = res3 + res1
            res4 = res4 + x1

        elif self.slink == 'H':
            # B的变体，但在向后累加时，丢掉本block的res1，只有res3.
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.conv2(F.relu(self.bn2(res1)))
            res3 = self.deconv3(F.relu(self.bn3(res2)))
            res4 = self.deconv4(F.relu(self.bn4(res3)))
            out = res2

            # res1 = res1 if x2 is None else res1 + x2
            res2 = res2 if x3 is None else res2 + x3
            res3 = res3 if x3 is None else res3 + x2
            res4 = res4 + x1

        elif self.slink == 'N':
            # No Shortcuts Used
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.conv2(F.relu(self.bn2(res1)))
            res3 = self.deconv3(F.relu(self.bn3(res2)))
            res4 = self.deconv4(F.relu(self.bn4(res3)))
            out = res2

        else:
            raise NotImplementedError('Unknown Slink for DoubleCouple : %s ' % self.slink)

        if self.classify > 0 and self.active_fc:
            out = self.classifier(out)
        else:
            out = None
        pred.append(out)

        if self.after:
            if self.down:
                res4 = self.down_res4(res4)
                res3 = self.down_res3(res3)
                res2 = self.down_res2(res2)
                return res4, res3, res2, pred
            else:
                return res4, res3, res2, pred
        else:
            if self.last_branch == 1:
                if self.last_down:
                    res4 = self.down_last4(res4)
                return res4, pred
            elif self.last_branch == 2:
                if self.last_down:
                    res4 = self.down_last4(res4)
                    res3 = self.down_last3(res3)
                return res4, res3, pred
            elif self.last_branch == 3:
                if self.last_down:
                    res4 = self.down_last4(res4)
                    res3 = self.down_last3(res3)
                    res2 = self.down_last2(res2)
                return res4, res3, res2, pred
            else:
                raise ValueError('<last_branch> of DoubleCouple should be <= 3!')


class SingleCouple(nn.Module):
    exp1 = 1  # exp1, exp2 is the internal expand of this block, set it >=1 will be ok.
    exp2 = 1
    exp3 = 2  # must==2, double the channel for the next down-size layer
    exp4 = 1  # maybe==2, == self.last_expand, have or not have channel expand for the last fc layer

    down_func = {'A': DownSampleA, 'B': DownSampleB, 'C': DownSampleC,
                 'D': DownSampleD, 'E': DownSampleE, 'H': DownSampelH}

    # A >> B >>> C , exp:22>>26>>>25, A extra Parameters

    def __init__(self, depth, after=True, down=False,
                 last_down=False, last_branch=1, last_expand=1,
                 classify=0, dfunc='A',
                 nclass=1000, blockexp=(1, 1), slink='A'):
        super(SingleCouple, self).__init__()
        assert last_branch <= 2, '<last_branch> of SingleCouple should be <= 2...'
        self.depth = depth
        self.after = after  # dose still have another DoubleCouple behind of this DoubleCouple. ?
        self.down = down  # dose connect to a same-size DoubleCouple or down-sized DoubleCouple. ?
        self.last_branch = last_branch  # how many output-ways (branches) for the classifier ?
        self.last_down = last_down  # dose down size for the classifier?
        self.last_expand = last_expand  # dose expand channel for the classifier
        self.slink = slink  # A >> C > B
        self.dfunc = dfunc
        self.down_func = self.down_func[dfunc]
        self.classify = classify
        self.exp1, self.exp2 = blockexp
        self.active_fc = False

        self.bn1 = nn.BatchNorm2d(depth)
        self.conv1 = nn.Conv2d(depth, depth * self.exp1, 3, stride=2, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(depth * self.exp1)
        self.deconv2 = nn.ConvTranspose2d(depth * self.exp1, depth, 4, stride=2, padding=1, bias=False,
                                          output_padding=0)

        if self.classify > 0:
            self.classifier = XClassifier(depth * self.exp2, nclass)

        if self.after:
            if self.down:
                self.down_res2 = self.down_func(depth, depth * self.exp3)
                self.down_res1 = self.down_func(depth * self.exp1, depth * self.exp3)
        else:
            if self.last_down:
                if self.last_branch >= 1:
                    self.down_last2 = self.down_func(depth, depth * self.last_expand)
                if self.last_branch >= 2:
                    self.down_last1 = self.down_func(depth * self.exp1, depth * self.last_expand)
            else:
                if self.classify > 0 and self.last_branch == 2:
                    # 此时，最后一个Couple的中间层被当做branch输出而对接在Summary上.
                    # 因此，删除此Couple自带的Classifier,以免与Summary中的Classifier重复.
                    delattr(self, 'classifier')
                    self.classify = 0
                    print('Note: 1 xfc will be deleted because of duplication!')

    def forward(self, x):
        # x3/res3 will be not used, but output 0 for parameters match between 2 layers
        if isinstance(x, tuple):
            x1, x2, x3, pred = x
            # print('x1, x2, x3: ', x1.size(), x2.size(), x3.size(), pred)
        else:
            x1, x2, x3, pred = x, None, None, None

        # add-style for use
        if self.slink == 'A':
            # 共包含1个夸Block连接，1个Block内连接.
            # first shorcut, then add,
            res1 = self.conv1(F.relu(self.bn1(x1)))
            out = res1 if x2 is None else res1 + x2
            res2 = self.deconv2(F.relu(self.bn2(out)))
            res2 = res2 if x1 is None else res2 + x1
            res3 = torch.Tensor(0).type_as(x3)

        elif self.slink == 'X':
            # 共包含1个夸Block连接，1个Block内连接
            # first add, then shorcut
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res1 = res1 if x2 is None else res1 + x2
            res2 = self.deconv2(F.relu(self.bn2(res1)))
            res2 = res2 if x1 is None else res2 + x1
            # res3 = torch.zeros_like(x3, dtype=x3.dtype, device=x3.device)
            res3 = torch.Tensor(0).type_as(x3)
            out = res1

        elif self.slink == 'B':
            # A的简化版，只有1个block内连接
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.deconv2(F.relu(self.bn2(res1)))
            res2 = res2 if x1 is None else res2 + x1
            res3 = torch.Tensor(0).type_as(x3)
            out = res1

        elif self.slink == 'C':
            # A的简化版，只有1个跨block连接
            res1 = self.conv1(F.relu(self.bn1(x1)))
            out = res1 if x2 is None else res1 + x2
            res2 = self.deconv2(F.relu(self.bn2(out)))
            res3 = torch.Tensor(0).type_as(x3)

        elif self.slink == 'D':
            # 夸Block的链接全部累加到最后一个Block内生效
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.deconv2(F.relu(self.bn2(res1)))
            res1 = res1 if x2 is None else res1 + x2
            res2 = res2 if x1 is None else res2 + x1
            res3 = torch.Tensor(0).type_as(x3)
            out = res1

        # cat-style no use
        elif self.slink == 'E':
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.deconv2(F.relu(self.bn2(res1)))
            res1 = res1 if x2 is None else torch.cat((res1, x2), 1)
            res2 = res2 if x1 is None else torch.cat((res2, x1), 1)
            res3 = torch.Tensor(0).type_as(x3)
            out = res1

        elif self.slink == 'N':
            # No Shortcuts Used
            res1 = self.conv1(F.relu(self.bn1(x1)))
            res2 = self.deconv2(F.relu(self.bn2(res1)))
            res3 = torch.Tensor(0).type_as(x3)
            out = res1

        else:
            raise NotImplementedError('Unknown Slink for SingleCouple : %s ' % self.slink)

        if self.classify > 0 and self.active_fc:
            out = self.classifier(out)
        else:
            out = None
        pred.append(out)

        if self.after:
            if self.down:
                res2 = self.down_res2(res2)
                res1 = self.down_res1(res1)
                return res2, res1, res3, pred
            else:
                return res2, res1, res3, pred
        else:
            if self.last_branch == 1:
                if self.last_down:
                    res2 = self.down_last2(res2)
                return res2, pred
            elif self.last_branch == 2:
                if self.last_down:
                    res2 = self.down_last2(res2)
                    res1 = self.down_last1(res1)
                return res2, res1, pred
            else:
                raise ValueError('<last_branch> of SingleCouple should be <= 2!')


class RockSummary(nn.Module):
    def __init__(self, indepth,  branch=1, active='relu', pool='avg', nclass=1000):
        super(RockSummary, self).__init__()
        self.indepth = indepth
        self.branch = branch
        self.active_fc = True
        self.pool = pool
        for b in range(1, branch + 1):
            layer = nn.Sequential(
                nn.BatchNorm2d(indepth),
                Activate(active),
                AdaAvgPool(),
                ViewLayer(),
                nn.Linear(indepth, nclass)
            )
            setattr(self, 'classifier%s' % b, layer)

    def forward(self, x):
        # x1, x2, x3 extracted form x is big, media, small respectively.
        # 为确保fc(xi)的顺序与layer_i在model内的顺序相一致和相对应，
        # so the output order should be [fc(x3), fc(x2), fc(x1)]
        if not self.active_fc:
            return x
        assert isinstance(x, (tuple, list)), 'x must be tuple, but %s' % type(x)
        assert len(x) == self.branch + 1, 'pred should be input together with x'
        x, pred = x[:-1][::-1], x[-1]
        # utils.print_size(x)
        for i, xi in enumerate(x):
            xi = getattr(self, 'classifier%s' % (i+1))(xi)
            pred.append(xi)
        return pred


class AutoBoost(nn.Module):
    # (xfc+lfc) x batchSize x nclass -> nfc x batchSize x nclass
    # bsize x nfc x nclass -> bsize x 1 x nclass -> bsize x nclass
    # '+': train xfc + boost together
    # '-': train first only xfc, then only boost, then tuning together
    # '*': no train only val, train only xfc, then 根据xfc的输出进行投票 boost
    METHOD = ('none', 'conv', 'soft', 'hard')
    # none-voting, conv-voting, soft-voting  hard-voting

    def __init__(self, xfc, lfc, ksize=1, nclass=1000, method='none'):
        super(AutoBoost, self).__init__()
        assert method in self.METHOD, 'Unknown Param <method> %s' % method
        self.xfc = xfc
        self.lfc = lfc
        self.ksize = ksize
        self.nclass = nclass
        self.method = method
        self.nfc = xfc + lfc
        self.active_fc = False
        if self.method == 'none':
            # 不做处理，直接返回
            pass
        elif self.method == 'conv':
            # 线性加权，Σnfc = fc
            self.conv = nn.Conv1d(self.nfc, 1, ksize, stride=1, padding=0, bias=False)
        elif self.method == 'soft':
            # 所有xfc按均值投票
            pass
        elif self.method == 'hard':
            # 所有xfc票多者胜
            pass

    def forward(self, x):
        if not self.active_fc:
            return x
        assert isinstance(x, (list, tuple))
        pred = []
        if self.method == 'none':
            return x
        elif self.method == 'conv':
            x = [xi.view(xi.size(0), 1, xi.size(1)) for xi in x]
            x = torch.cat(x, dim=1)
            x = self.conv(x)
            x = x.view(x.size(0), -1)
            pred.append(x)
        elif self.method == 'soft':
            x = sum(x) / len(x)
            pred.append(x)
        elif self.method == 'hard':
            pass
        else:
            raise NotImplementedError('Unknown Param <method> : %s' % self.method)
        return pred


class AEResNetMix(nn.Module):
    couple = {'D': DoubleCouple, 'S': SingleCouple}
    rocker = {'A': RockBlockA, 'B': RockBlockB, 'C': RockBlockC, 'M': RockBlockM,
              'D': RockBlockD, 'E': RockBlockE, 'F': RockBlockF, 'H': RockBlockH}

    def __init__(self, branch=3, rock='A', depth=64, blockexp=(1, 1), stages=4,
                 layers=(2, 2, 2, 2),
                 blocks=('D', 'D', 'D', 'D'),
                 slink=('A', 'A', 'A', 'A'),
                 expand=(1, 2, 4, 8),
                 dfunc=('D', 'D', 'D', 'D'),
                 classify=(1, 1, 1, 1),
                 fcboost='none',
                 last_branch=1,
                 last_down=True,
                 last_expand=1,
                 kldloss=False,
                 nclass=1000):
        super(AEResNetMix, self).__init__()
        assert stages <= min(len(blocks), len(slink), len(expand),
                             len(layers), len(dfunc), len(classify)), \
            'Hyper Pameters Not Enough to Match Stages Nums:%s!' % stages
        assert sorted(blocks[:stages]) == list(blocks[:stages]), \
            'DoubleCouple must be ahead of SingleCouple! %s' % blocks[:stages]

        dataset = ['imagenet', 'cifar'][nclass != 1000]
        imgsize = [224, 32][nclass != 1000]
        if dataset == 'cifar':
            assert stages <= 4, 'cifar stages should <= 4'
        elif dataset == 'imagenet':
            assert stages <= 5, 'imagenet stages should <= 5'

        self.branch = branch
        self.rock = self.rocker[rock]
        self.depth = depth
        self.blockexp = blockexp
        self.stages = stages
        self.blocks = blocks    # [self.couple[b] for b in blocks]
        self.slink = slink
        self.expand = expand
        self.layers = layers
        self.dfunc = dfunc
        self.classify = classify
        self.fcboost = fcboost
        self.last_branch = last_branch
        self.last_down = last_down
        self.last_expand = last_expand
        self.kldloss = kldloss
        self.nclass = nclass
        self.active_boost = False

        self.after = [True for _ in range(stages - 1)] + [False]
        fc_indepth = depth * expand[stages - 1]
        if last_down: fc_indepth *= last_expand

        self.stage0 = self.rock(depth=depth, branch=branch, dataset=dataset)
        for i in range(stages):
            layer = self._make_aelayer(self.couple[blocks[i]], layers[i], expand[i] * depth,
                                       slink[i], self.after[i], last_branch, last_down, last_expand,
                                       dfunc=dfunc[i], cfy=classify[i], blockexp=blockexp)
            setattr(self, 'stage%s' % (i + 1), layer)
        self.summary = RockSummary(fc_indepth, last_branch, active='relu', nclass=nclass)
        xfc_nums = sum([1 for n, m in self.named_modules()
                        if isinstance(m, (DoubleCouple, SingleCouple))
                        and hasattr(m, 'classifier')])
        self.boost = AutoBoost(xfc=xfc_nums, lfc=last_branch, ksize=1, nclass=nclass, method=fcboost)

        if kldloss:
            self.kld_criterion = nn.KLDivLoss()

        self.train_which_now = {'conv+rock': False, 'xfc+boost': False,
                                'xfc-only': False, 'boost-only': False}
        self.eval_which_now = {'conv+rock': False, 'conv+rock+xfc': False,
                               'conv+rock+boost': False, 'conv+rock+xfc+boost': False}

        self._init_params()

    def _make_aelayer(self, block, block_nums, indepth, slink, after,
                      last_branch, last_down, last_expand, dfunc, cfy=0,
                      blockexp=(1, 1)):
        # last_branch & last_down & last_expand work only when after=False;
        # if after=True, their values have no influence.
        layers = []
        for i in range(block_nums - 1):
            layers.append(
                block(depth=indepth, after=True, down=False,
                      last_branch=last_branch, last_down=False, last_expand=1,
                      dfunc=dfunc, classify=cfy, nclass=self.nclass, blockexp=blockexp, slink=slink))
        layers.append(
            block(depth=indepth, after=after, down=True,
                  last_branch=last_branch, last_down=last_down, last_expand=last_expand,
                  dfunc=dfunc, classify=cfy, nclass=self.nclass, blockexp=blockexp, slink=slink))
        return nn.Sequential(*layers)

    def _init_params(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
                # m.bias.data.zero_()
            # elif isinstance(m, nn.Conv1d):
            #     n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
            #     m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                nn.init.kaiming_normal_(m.weight)
                m.bias.data.zero_()
        # TODO  CONV1D....

    def forward(self, x):
        x = self.stage0(x)
        for s in range(self.stages):
            x = getattr(self, 'stage%s' % (s + 1))(x)
        x = self.summary(x)     # x => pred
        x = [p for p in x if p is not None]
        x = self.boost(x)
        return x

    def forward2(self, x):
        # deperecated: the same logical to self.forward
        pred = []
        x = self.stage0(x)
        if self.stages >= 1:
            x = self.stage1(x)
            if not self.after[0]:
                x, pred = x
        if self.stages >= 2:
            x = self.stage2(x)
            if not self.after[1]:
                x, pred = x
        if self.stages >= 3:
            x = self.stage3(x)
            if not self.after[2]:
                x, pred = x
        if self.stages >= 4:
            x = self.stage4(x)
            if not self.after[3]:
                x, pred = x
        if self.stages >= 5:
            x = self.stage5(x)
            if not self.after[4]:
                x, pred = x
        x = self.classifier(x)
        pred.append(x)
        pred = [p for p in pred if p is not None]
        return pred

    def kld_loss(self, pred, by='last', issum=True):
        if by == 'last':
            refer = pred[-1]
        elif by == 'mean':
            mean = pred[0]
            for p in pred[1:]:
                mean += p
            refer = mean / len(pred)
        else:
            raise NotImplementedError
        kld_loss = [self.kld_criterion(p, refer) for p in pred]
        if issum:
            kld_loss = sum(kld_loss)
        return kld_loss

    def train_which(self, part='conv+rock'):
        # if self.train_which_now[part]:
        #     return
        if part == 'conv+rock':
            self.train()
            for name, module in self.named_modules():
                if isinstance(module, (DoubleCouple, SingleCouple)):
                    module.active_fc = False
                    fclayer = getattr(module, 'classifier', nn.ReLU())
                    fclayer.eval()
                    for p in fclayer.parameters():
                        p.requires_grad = False
                if isinstance(module, AutoBoost):
                    module.active_fc = False
                    module.eval()
                    for p in module.parameters():
                        p.requires_grad = False
        elif part == 'xfc+boost':
            # self.eval()
            for name, module in self.named_modules():
                if isinstance(module, (DoubleCouple, SingleCouple)):
                    module.active_fc = True
                    for n, m in module.named_modules():
                        if 'classifier' in n:
                            m.train()
                        else:
                            m.eval()
                    for n, p in module.named_parameters():
                        if 'classifier' in n:
                            p.requires_grad = True
                        else:
                            p.requires_grad = False
                if isinstance(module, AutoBoost):
                    module.train()
                    module.active_fc = True
                    for p in module.parameters():
                        p.requires_grad = True
                if isinstance(module, (RockBlock, RockSummary)):
                    module.eval()
                    for p in module.parameters():
                        p.requires_grad = False
        elif part == 'xfc-only':
            for name, module in self.named_modules():
                if isinstance(module, (DoubleCouple, SingleCouple)):
                    module.active_fc = True
                    for n, m in module.named_modules():
                        if 'classifier' in n:
                            m.train()
                        else:
                            m.eval()
                    for n, p in module.named_parameters():
                        if 'classifier' in n:
                            p.requires_grad = True
                        else:
                            p.requires_grad = False
                if isinstance(module, AutoBoost):
                    module.eval()
                    module.active_fc = False
                    for p in module.parameters():
                        p.requires_grad = False
                if isinstance(module, (RockBlock, RockSummary)):
                    module.eval()
                    for p in module.parameters():
                        p.requires_grad = False
        elif part == 'boost-only':
            for name, module in self.named_modules():
                if isinstance(module, (DoubleCouple, SingleCouple)):
                    module.active_fc = True
                    module.eval()
                    for n, p in module.named_parameters():
                        p.requires_grad = False
                if isinstance(module, AutoBoost):
                    module.train()
                    module.active_fc = True
                    for p in module.parameters():
                        p.requires_grad = True
                if isinstance(module, (RockBlock, RockSummary)):
                    module.eval()
                    for p in module.parameters():
                        p.requires_grad = False
        else:
            raise NotImplementedError('Unknown Param <part> : %s' % part)
        for key in self.train_which_now.keys():
            self.train_which_now[key] = True if key == part else False
        print('model.train_which_now', self.train_which_now)

    def eval_which(self, part='conv+rock'):
        # if self.eval_which_now[part]:
        #     return
        if part == 'conv+rock':
            self.eval()
            for name, module in self.named_modules():
                if isinstance(module, (DoubleCouple, SingleCouple)):
                    module.active_fc = False
                if isinstance(module, AutoBoost):
                    module.active_fc = False
        elif part == 'conv+rock+xfc':
            self.eval()
            for name, module in self.named_modules():
                if isinstance(module, (DoubleCouple, SingleCouple)):
                    module.active_fc = True
                if isinstance(module, AutoBoost):
                    module.active_fc = False
        elif part == 'conv+rock+boost':
            assert sum(self.classify) == 0, '此情况下xfc数量必须为零，否则<xfc_nums>无法唯一确定！'
            self.eval()
            for name, module in self.named_modules():
                if isinstance(module, (DoubleCouple, SingleCouple)):
                    module.active_fc = False
                if isinstance(module, AutoBoost):
                    module.active_fc = True
        elif part == 'conv+rock+xfc+boost':
            self.eval()
            for name, module in self.named_modules():
                if isinstance(module, (DoubleCouple, SingleCouple)):
                    module.active_fc = True
                if isinstance(module, AutoBoost):
                    module.active_fc = True
        for key in self.eval_which_now.keys():
            self.eval_which_now[key] = True if key == part else False
        print('model.eval_which_now', self.eval_which_now)


class CifarAEResNetMix(nn.Module):
    # 结构与AEResNetMix一致，可以用之代替
    couple = {'D': DoubleCouple, 'S': SingleCouple}
    rocker = {'A': RockBlockA, 'B': RockBlockB, 'C': RockBlockC, 'M': RockBlockM,
              'D': RockBlockD, 'E': RockBlockE, 'F': RockBlockF, 'H': RockBlockH}
    image_size = 32

    def __init__(self, branch=3, rock='A', depth=16, blockexp=(1, 1), stages=3,
                 blocks=('D', 'D', 'S'),
                 slink=('A', 'A', 'A'),
                 expand=(1, 2, 4),
                 layers=(2, 2, 2),
                 dfunc=('C', 'C', 'C'),
                 classify=(1, 1, 1),
                 last_down=True,
                 last_expand=1,
                 nclass=10):
        super(CifarAEResNetMix, self).__init__()
        assert stages <= min(len(blocks), len(slink), len(expand),
                             len(layers), len(dfunc), len(classify)), \
            'Hyper Pameters Not Enough to Match Stages:%s!' % stages
        assert sorted(blocks[:stages]) == list(blocks[:stages]), \
            'DoubleCouple must be ahead of SingleCouple! %s' % blocks[:stages]
        assert stages <= 4, 'cifar stages should < 4'
        self.branch = branch
        self.rock = self.rocker[rock]
        self.depth = depth
        self.expand = expand
        self.layers = layers
        self.dfunc = dfunc
        self.classify = classify
        self.last_down = last_down
        self.last_expand = last_expand
        self.stages = stages
        self.blocks = blocks
        self.slink = slink
        self.nclass = nclass

        self.after = [True for _ in range(stages - 1)] + [False]
        fc_indepth = depth * expand[stages - 1]
        if self.last_down:
            fc_indepth *= last_expand

        self.layer0 = self.rock(depth=depth, branch=branch, dataset='cifar')
        if stages >= 1:
            self.stage1 = self._make_aelayer(self.couple[blocks[0]], layers[0], expand[0] * depth, slink[0],
                                             self.after[0], last_down, last_expand, dfunc=dfunc[0],
                                             cfy=classify[0], blockexp=blockexp)
        if stages >= 2:
            self.stage2 = self._make_aelayer(self.couple[blocks[1]], layers[1], expand[1] * depth, slink[1],
                                             self.after[1], last_down, last_expand, dfunc=dfunc[1],
                                             cfy=classify[1], blockexp=blockexp)
        if stages >= 3:
            self.stage3 = self._make_aelayer(self.couple[blocks[2]], layers[2], expand[2] * depth, slink[2],
                                             self.after[2], last_down, last_expand, dfunc=dfunc[2],
                                             cfy=classify[2], blockexp=blockexp)
        if stages >= 4:
            self.stage4 = self._make_aelayer(self.couple[blocks[3]], layers[3], expand[3] * depth, slink[3],
                                             self.after[3], last_down, last_expand, dfunc=dfunc[3],
                                             cfy=classify[3], blockexp=blockexp)
        self.classifier = nn.Sequential(
            nn.BatchNorm2d(fc_indepth),
            nn.ReLU(),
            AdaAvgPool(),
            ViewLayer(dim=-1),
            nn.Linear(fc_indepth, nclass))

        self._init_params()

    def _make_aelayer(self, block, block_nums, indepth, slink, after, last_down, last_expand, dfunc, cfy=0,
                      blockexp=(1, 1)):
        layers = []
        for i in range(block_nums - 1):
            layers.append(
                block(depth=indepth, after=True, down=False, last_down=False, last_expand=1,
                      dfunc=dfunc, classify=cfy, nclass=self.nclass, blockexp=blockexp, slink=slink))
        # last_down & last_expand work only when after=False; if after=True, their values have no influence.
        layers.append(
            block(depth=indepth, after=after, down=True, last_down=last_down, last_expand=last_expand,
                  dfunc=dfunc, classify=cfy, nclass=self.nclass, blockexp=blockexp, slink=slink))
        return nn.Sequential(*layers)

    def _init_params(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
                # m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                nn.init.kaiming_normal(m.weight)
                m.bias.data.zero_()

    def forward(self, x):
        pred = []
        x = self.layer0(x)
        if self.stages >= 1:
            x = self.stage1(x)
            if not self.after[0]:
                x, pred = x
        if self.stages >= 2:
            x = self.stage2(x)
            if not self.after[1]:
                x, pred = x
        if self.stages >= 3:
            x = self.stage3(x)
            if not self.after[2]:
                x, pred = x
        if self.stages >= 4:
            x = self.stage4(x)
            if not self.after[3]:
                x, pred = x
        x = self.classifier(x)
        pred.append(x)
        pred = [p for p in pred if p is not None]
        return pred


if __name__ == '__main__':
    import xtils

    torch.manual_seed(1)

    # # imageNet
    #
    # exp5 = {'branch': 3, 'rock': 'M', 'depth': 64, 'blockexp': (1, 1), 'stages': 5, 'nclass': 1000,
    #         'layers': (2, 2, 2, 2, 2), 'blocks': ('D', 'D', 'D', 'D', 'D'), 'slink': ('A', 'A', 'A', 'A', 'A'),
    #         'expand': (1, 2, 4, 8, 16), 'dfunc': ('D', 'D', 'D', 'D', 'D'), 'classify': (1, 1, 1, 1, 1),
    #         'fcboost': 'none', 'last_branch': 1, 'last_down': False, 'last_expand': 2}
    # exp4 = {'branch': 3, 'rock': 'M', 'depth': 64, 'blockexp': (1, 1), 'stages': 4, 'nclass': 1000,
    #         'layers': (7, 5, 3, 1), 'blocks': ('D', 'D', 'D', 'D'), 'slink': ('A', 'A', 'A', 'A'),
    #         'expand': (1, 2, 4, 8), 'dfunc': ('D', 'D', 'D', 'D'), 'classify': (0, 0, 0, 0), 'fcboost': 'none',
    #         'last_branch': 3, 'last_down': True, 'last_expand': 2}
    # exp3 = {'branch': 3, 'rock': 'M', 'depth': 64, 'blockexp': (1, 1), 'stages': 3, 'nclass': 1000,
    #         'layers': (3, 3, 3), 'blocks': ('D', 'D', 'S'), 'slink': ('A', 'A', 'A'),
    #         'expand': (1, 2, 4), 'dfunc': ('D', 'D', 'D'), 'classify': (1, 1, 0), 'fcboost': 'conv',
    #         'last_branch': 2, 'last_down': True, 'last_expand': 2}
    #
    # model = AEResNetMix(**exp3)
    # print('\n', model, '\n')
    #
    # # train_which & eval_which 在组合上必须相互匹配
    # # model.train_which(part=['conv+rock', 'xfc+boost', 'xfc-only', 'boost-only'][1])
    # model.eval_which(part=['conv+rock', 'conv+rock+xfc', 'conv+rock+boost', 'conv+rock+xfc+boost'][1])
    # # print(model.stage1[1].conv1.training)
    # # print(model.stage1[1].classifier.training)
    # # print(model.stage2[0].classifier.training)
    # # print(model.summary.classifier1.training)
    #
    # x = torch.randn(4, 3, 256, 256)
    # # utils.tensorboard_add_model(model, x)
    # utils.calculate_params_scale(model, format='million')
    # utils.calculate_layers_num(model, layers=('conv2d', 'deconv2d', 'fc'))
    # y = model(x)
    # print('有效分类支路：', len(y), '\t共有blocks：', sum(model.layers))
    # print(':', [yy.shape for yy in y if yy is not None])
    # print(':', [yy.max(1) for yy in y if yy is not None])
    # # print('\n xxxxxxxxxxxxxxxxx \n')
    # # for n, m in model.named_modules(prefix='stage'):
    # #     print(n,'-->', m)
    # #     if hasattr(m, 'active_fc'):
    # #         print('OK-->', n, m)
    # #     if isinstance(m, (DoubleCouple, SingleCouple)):
    # #         m.active_fc = False

    # cifar10
    exp4 = {'branch': 3, 'rock': 'M', 'depth': 20, 'blockexp': (1, 1), 'stages': 4,
            'layers': (2, 2, 2, 2), 'blocks': ('D', 'D', 'S', 'S'), 'slink': ('A', 'A', 'A', 'A'),
            'expand': (1, 2, 4, 8), 'dfunc': ('D', 'D', 'D', 'D'), 'classify': (1, 1, 1, 1),
            'last_down': False, 'last_expand': 1, 'nclass': 10}

    exp3 = {'branch': 3, 'rock': 'A', 'depth': 16, 'blockexp': (1, 1), 'stages': 3,
            'layers': (2, 2, 2), 'blocks': ('D', 'D', 'D'), 'slink': ('A', 'A', 'A'),
            'expand': (1, 2, 4), 'dfunc': ('D', 'D', 'D'), 'classify': (1, 1, 1),
            'last_down': True, 'last_expand': 1, 'nclass': 10}

    exp2 = {'branch': 3, 'rock': 'M', 'depth': 16, 'blockexp': (1, 1), 'slink': ('A', 'A', '0'), 'stages': 2,
            'blocks': ('D', 'S', '0'), 'expand': (1, 2, 0), 'layers': (30, 10, 0), 'dfunc': ('D', 'D', '0'),
            'classify': (0, 0, 0), 'last_down': True, 'last_expand': 2, 'nclass': 10}

    model = [CifarAEResNetMix, AEResNetMix][1](**exp2)
    print('\n', model, '\n')
    x = torch.randn(4, 3, 32, 32)
    # utils.tensorboard_add_model(model, x)
    xtils.calculate_params_scale(model, format='million')
    xtils.calculate_layers_num(model, layers=('conv2d', 'deconv2d', 'fc'))
    y = model(x)
    print(len(y), sum(model.layers), ':', [(yy.shape, yy.max(1)) for yy in y if yy is not None])
