# coding=utf-8
import copy
from utils import utils
import numpy as np
import logging
import pandas as pd
from tqdm import tqdm
import torch
from collections import defaultdict
from data_processor.DataProcessor import DataProcessor
from utils import global_p


class HisDataProcessor(DataProcessor):
    data_columns = ['X', global_p.C_HISTORY, global_p.C_HISTORY_LENGTH]

    @staticmethod
    def parse_dp_args(parser):
        """
        parse data processor related arguments
        """
        parser.add_argument('--max_his', type=int, default=-1,
                            help='Max history length.')
        parser.add_argument('--sup_his', type=int, default=0,
                            help='If sup_his > 0, supplement history list with -1 at the beginning')
        parser.add_argument('--sparse_his', type=int, default=1,
                            help='Whether use sparse representation of user history.')
        return DataProcessor.parse_dp_args(parser)

    def __init__(self, data_loader, model, rank, test_neg_n, max_his, sup_his, sparse_his):
        DataProcessor.__init__(self, data_loader=data_loader, model=model, rank=rank, test_neg_n=test_neg_n)
        self.max_his = max_his
        self.sparse_his = sparse_his
        self.sup_his = sup_his
        self.boolean_test_data = None

    def _get_feed_dict_rt(self, data, batch_start, batch_size, train):
        """
        generate a batch for rating/clicking prediction
        :param data: data dict，generated by self.get_*_data() and self.format_data_dict()
        :param batch_start: start index of current batch
        :param batch_size: batch size
        :param train: train or validation/test
        :return: batch的feed dict
        """
        batch_end = min(len(data['X']), batch_start + batch_size)
        real_batch_size = batch_end - batch_start
        feed_dict = {'train': train, 'rank': 0,
                     global_p.K_SAMPLE_ID: data[global_p.K_SAMPLE_ID][batch_start:batch_start + real_batch_size]}
        if 'Y' in data:
            feed_dict['Y'] = utils.numpy_to_torch(data['Y'][batch_start:batch_start + real_batch_size])
        else:
            feed_dict['Y'] = utils.numpy_to_torch(np.zeros(shape=real_batch_size))
        for c in self.data_columns:
            d = data[c][batch_start: batch_start + real_batch_size]
            if c == global_p.C_HISTORY and self.sparse_his == 1:
                x, y = [], []
                for idx, iids in enumerate(d):
                    x.extend([idx] * len(iids))
                    y.extend(iids)
                if len(x) <= 0:
                    i = torch.LongTensor([[0], [0]])
                    v = torch.FloatTensor([0.0])
                else:
                    i = torch.LongTensor([x, y])
                    v = torch.FloatTensor([1.0] * len(x))
                history = torch.sparse.FloatTensor(i, v, torch.Size([real_batch_size, self.data_loader.item_num]))
                if torch.cuda.device_count() > 0:
                    history = history.cuda()
                feed_dict[c] = history
            else:
                feed_dict[c] = utils.numpy_to_torch(d)
        return feed_dict

    def _get_feed_dict_rk(self, data, batch_start, batch_size, train, neg_data=None):
        if not train:
            feed_dict = self._get_feed_dict_rt(
                data=data, batch_start=batch_start, batch_size=batch_size, train=train)
            feed_dict['rank'] = 1
        else:
            batch_end = min(len(data['X']), batch_start + batch_size)
            real_batch_size = batch_end - batch_start
            neg_columns_dict = {}
            if neg_data is None:
                logging.warning('neg_data is None')
                neg_df = self.generate_neg_df(
                    uid_list=data['uid'][batch_start: batch_start + real_batch_size],
                    iid_list=data['iid'][batch_start: batch_start + real_batch_size],
                    df=self.data_loader.train_df, neg_n=1, train=True)
                neg_data = self.format_data_dict(neg_df)
                for c in self.data_columns:
                    neg_columns_dict[c] = neg_data[c]
            else:
                for c in self.data_columns:
                    neg_columns_dict[c] = neg_data[c][batch_start: batch_start + real_batch_size]
            y = np.concatenate([np.ones(shape=real_batch_size, dtype=np.float32),
                                np.zeros(shape=real_batch_size, dtype=np.float32)])
            sample_id = data[global_p.K_SAMPLE_ID][batch_start:batch_start + real_batch_size]
            neg_sample_id = sample_id + len(self.train_data['Y'])
            feed_dict = {
                'train': train, 'rank': 1,
                'Y': utils.numpy_to_torch(y),
                global_p.K_SAMPLE_ID: np.concatenate([sample_id, neg_sample_id])}
            for c in self.data_columns:
                d = np.concatenate([data[c][batch_start: batch_start + real_batch_size], neg_columns_dict[c]])
                if c == global_p.C_HISTORY and self.sparse_his == 1:
                    x, y = [], []
                    for idx, iids in enumerate(d):
                        x.extend([idx] * len(iids))
                        y.extend(iids)
                    if len(x) <= 0:
                        i = torch.LongTensor([[0], [0]])
                        v = torch.FloatTensor([0.0])
                    else:
                        i = torch.LongTensor([x, y])
                        v = torch.FloatTensor([1.0] * len(x))
                    history = torch.sparse.FloatTensor(
                        i, v, torch.Size([real_batch_size * 2, self.data_loader.item_num]))
                    if torch.cuda.device_count() > 0:
                        history = history.cuda()
                    feed_dict[c] = history
                else:
                    feed_dict[c] = utils.numpy_to_torch(d)
        return feed_dict

    def _prepare_batches_rt(self, data, batch_size, train):
        if self.sparse_his == 1 or self.sup_his == 1:
            return DataProcessor._prepare_batches_rt(self, data=data, batch_size=batch_size, train=train)

        if data is None:
            return None
        num_example = len(data['X'])
        assert num_example > 0

        # group data by their history length
        length_dict = {}
        lengths = [len(x) for x in data[global_p.C_HISTORY]]
        for idx, l in enumerate(lengths):
            if l not in length_dict:
                length_dict[l] = []
            length_dict[l].append(idx)
        lengths = list(length_dict.keys())

        batches = []
        for l in tqdm(lengths, leave=False, ncols=100, mininterval=1, desc='Prepare Batches'):
            rows = length_dict[l]
            tmp_data = {}
            for key in data:
                if data[key].dtype == np.object:
                    tmp_data[key] = np.array([np.array(data[key][r]) for r in rows])
                else:
                    tmp_data[key] = data[key][rows]
            tmp_total_batch = int((len(rows) + batch_size - 1) / batch_size)
            for batch in range(tmp_total_batch):
                batches.append(self._get_feed_dict_rt(tmp_data, batch * batch_size, batch_size, train))
        np.random.shuffle(batches)
        return batches

    def _prepare_batches_rk(self, data, batch_size, train):
        if self.sparse_his == 1 or self.sup_his == 1:
            return DataProcessor._prepare_batches_rk(self, data=data, batch_size=batch_size, train=train)

        if data is None:
            return None
        num_example = len(data['X'])
        assert num_example > 0

        neg_data = None
        if train:
            neg_df = self.generate_neg_df(
                uid_list=data['uid'], iid_list=data['iid'],
                df=self.data_loader.train_df, neg_n=1, train=True)
            neg_data = self.format_data_dict(neg_df)

        length_dict = {}
        lengths = [len(x) for x in data[global_p.C_HISTORY]]
        for idx, l in enumerate(lengths):
            if l not in length_dict:
                length_dict[l] = []
            length_dict[l].append(idx)
        lengths = list(length_dict.keys())

        batches = []
        for l in tqdm(lengths, leave=False, ncols=100, mininterval=1, desc='Prepare Batches'):
            rows = length_dict[l]
            tmp_data = {}
            for key in data:
                if data[key].dtype == np.object:
                    tmp_data[key] = np.array([np.array(data[key][r]) for r in rows])
                else:
                    tmp_data[key] = data[key][rows]
            tmp_neg_data = {} if train else None
            if train:
                for key in self.data_columns:
                    if data[key].dtype == np.object:
                        tmp_neg_data[key] = np.array([np.array(neg_data[key][r]) for r in rows])
                    else:
                        tmp_neg_data[key] = neg_data[key][rows]
            tmp_total_batch = int((len(rows) + batch_size - 1) / batch_size)
            for batch in range(tmp_total_batch):
                batches.append(self._get_feed_dict_rk(
                    tmp_data, batch * batch_size, batch_size, train, neg_data=tmp_neg_data))
        np.random.shuffle(batches)
        return batches

    def format_data_dict(self, df):

        if global_p.C_HISTORY in df:
            history = df[[global_p.C_HISTORY]]
        else:
            uids = df[['uid']]
            history = pd.merge(uids, self.data_loader.train_his_df, on='uid', how='left')
            history = history.rename(columns={'iids': global_p.C_HISTORY})

        history[global_p.C_HISTORY] = history[global_p.C_HISTORY].fillna('')
        data_dict = DataProcessor.format_data_dict(self, df)
        if self.max_his > 0 and self.sup_his == 1:
            # 如果max_his > 0 self.sup_his==1，则从末尾截取max_his长度的历史，不够的在开头补齐-1
            data_dict[global_p.C_HISTORY] = history[global_p.C_HISTORY]. \
                apply(lambda x: np.array(([-1] * self.max_his + [int(i) for i in x.split(',')])[-self.max_his:]) \
                if x != '' else np.array([])).values
        elif self.max_his > 0 and self.sup_his == 0:
            data_dict[global_p.C_HISTORY] = history[global_p.C_HISTORY]. \
                apply(lambda x: np.array([int(i) for i in x.split(',')][-self.max_his:]) if x != '' else np.array([])). \
                values
        else:
            data_dict[global_p.C_HISTORY] = history[global_p.C_HISTORY].apply(
                lambda x: [int(i) for i in x.split(',')] if x != '' else np.array([])).values
        data_dict[global_p.C_HISTORY_LENGTH] = np.array([len(h) for h in data_dict[global_p.C_HISTORY]])
        # print(data_dict[global_p.C_HISTORY])
        return data_dict