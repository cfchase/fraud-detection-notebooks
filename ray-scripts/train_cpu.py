import os
import torch
import torch.nn as nn
import ray
import pandas as pd

from pyarrow import fs
import pyarrow.csv as pv

from torch.utils.data import Dataset, DataLoader
from ray.train.torch import TorchTrainer
from ray.train import RunConfig, ScalingConfig

feature_indexes = [
    1,  # distance_from_last_transaction
    2,  # ratio_to_median_purchase_price
    4,  # used_chip
    5,  # used_pin_number
    6,  # online_order
]

label_indexes = [7]


# like scikit learn standard scaler
class TorchStandardScaler:
    def __init__(self):
        self.mean = None
        self.std = None

    def fit(self, tensor):
        self.mean = tensor.mean(dim=0, keepdim=False)
        self.std = tensor.std(dim=0, keepdim=False)

    def transform(self, tensor):
        return (tensor - self.mean) / self.std

    def fit_transform(self, tensor):
        self.fit(tensor)
        return self.transform(tensor)


class CSVDataset(Dataset):
    def __init__(self, csv_file, transform=None, target_transform=None):
        self.device = "cpu"
        self.feature_indexes = [
            1,  # distance_from_last_transaction
            2,  # ratio_to_median_purchase_price
            4,  # used_chip
            5,  # used_pin_number
            6,  # online_order
        ]

        self.label_indexes = [7]

        # self.feature_columns = feature_columns
        # self.label_column = label_column
        self.data = pd.read_csv(csv_file)
        self.transform = transform
        self.target_transform = target_transform

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        features = torch.tensor(self.data.iloc[idx, self.feature_indexes],
                                dtype=torch.float32).to(self.device)
        label = torch.tensor(self.data.iloc[idx, self.label_indexes],
                             dtype=torch.float32).to(self.device)
        # features = torch.tensor(self.data.loc[idx, self.feature_columns],
        #                         dtype=torch.float32).to(device)
        # label = torch.tensor(self.data.loc[idx, self.label_column],
        #                      dtype=torch.float32)
        if self.transform:
            features = self.transform(features)
        if self.target_transform:
            label = self.target_transform(label)

        return features, label


class NeuralNetwork(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear_relu_stack = nn.Sequential(
            nn.Linear(5, 8),
            nn.ReLU(),
            nn.Linear(8, 4),
            nn.ReLU(),
            nn.Linear(4, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        logits = self.linear_relu_stack(x)
        return logits


def get_datasets(fs):
    with fs.open_input_file("my-storage/data/card_transdata.csv") as file:
        # Read the CSV file into a PyArrow Table
        training_table = pv.read_csv(file)
    train_df = training_table.to_pandas()
    train_df = train_df.iloc[:, feature_indexes]
    train_df_tensor = torch.tensor(train_df.values, dtype=torch.float)

    train_df_tensor = torch.tensor(train_df.values, dtype=torch.float)
    scaler = TorchStandardScaler()
    scaler.fit(train_df_tensor)

    training_data = CSVDataset('data/train.csv', transform=scaler.transform)
    return training_data


def get_loss_fn(fs):
    with fs.open_input_file("my-storage/data/card_transdata.csv") as file:
        training_table = pv.read_csv(file)
    train_df = training_table.to_pandas()
    labels_df = train_df.iloc[:, label_indexes]
    labels_df_tensor = torch.tensor(labels_df.values, dtype=torch.float)
    positives = torch.sum(labels_df_tensor)
    negatives = (len(labels_df_tensor) - torch.sum(labels_df_tensor))
    pos_weight = torch.unsqueeze((negatives / positives), 0)
    print(pos_weight)

    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    return loss_fn


def get_fs():
    aws_access_key_id = os.environ.get('AWS_ACCESS_KEY_ID')
    aws_secret_access_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    endpoint_url = os.environ.get('AWS_S3_ENDPOINT')
    region_name = os.environ.get('AWS_DEFAULT_REGION')

    return fs.S3FileSystem(access_key=aws_access_key_id,
                         secret_key=aws_secret_access_key,
                         region=region_name,
                         endpoint_override=endpoint_url)


def train_func_distributed():
    pyarrow_fs = get_fs()
    num_epochs = 1
    batch_size = 10000

    training_data = get_datasets(pyarrow_fs)
    training_dataloader = DataLoader(training_data, batch_size=batch_size)
    training_dataloader = ray.train.torch.prepare_data_loader(training_dataloader)

    model = NeuralNetwork()
    model = ray.train.torch.prepare_model(model)

    loss_fn = get_loss_fn(pyarrow_fs)
    optimizer = torch.optim.Adam(params=model.parameters(), lr=0.01)

    for epoch in range(num_epochs):
        if ray.train.get_context().get_world_size() > 1:
            training_dataloader.sampler.set_epoch(epoch)

        for inputs, labels in training_dataloader:
            optimizer.zero_grad()
            pred = model(inputs)
            loss = loss_fn(pred, labels)
            loss.backward()
            optimizer.step()
        print(f"epoch: {epoch}, loss: {loss.item()}")


use_gpu = False
bucket_name = os.environ.get('AWS_S3_BUCKET')

trainer = TorchTrainer(
    train_func_distributed,
    run_config=RunConfig(
        storage_filesystem=get_fs(),
        storage_path=f"{bucket_name}/ray/",
        name="fraud-training",
    ),
    scaling_config=ScalingConfig(
        num_workers=3,   # num_workers = number of worker nodes with the ray head node included
        use_gpu=use_gpu,
    ),
)

results = trainer.fit()
