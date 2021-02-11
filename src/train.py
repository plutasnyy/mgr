import os
from configparser import ConfigParser
from copy import deepcopy
from pathlib import Path

import click
import pandas as pd
from easydict import EasyDict

os.environ['COMET_DISABLE_AUTO_LOGGING'] = '1'

from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning.callbacks import ModelCheckpoint, LearningRateMonitor
from pytorch_lightning.loggers import CometLogger
from sklearn.model_selection import train_test_split, StratifiedKFold
from torch.utils.data import DataLoader

from configs import model_data, dataset_tokens_length
from datasets import SentimentDataset
from models.transformer_lit import TransformerLitModule


@click.command()
@click.option('-n', '--name', required=True, type=str)
@click.option('-ds', '--data-set', required=True,
              type=click.Choice(['amazon', 'hotel', 'imdb', 'yelp', 'rottentomatoes']))
@click.option('-m', '--model', default='distilbert', type=click.Choice(['distilbert']))
@click.option('-l', '--length', default=None, type=int, help='If none use value from configs')
@click.option('--logger/--no-logger', default=True)
@click.option('-e', '--epoch', default=4, type=int)
@click.option('-f', '--fold', default=1, type=int)
@click.option('--lr', default=2e-5, type=float)
@click.option('--find-lr', default=False, is_flag=True)
@click.option('--seed', default=0, type=int)
@click.option('-bs', '--batch-size', default=32, type=int)
@click.option('-fdr', '--fast-dev-run', default=False, type=int)
def train(**params):
    params = EasyDict(params)
    seed_everything(params.seed)

    config = ConfigParser()
    config.read('config.ini')

    if params.length is None:
        params.length = dataset_tokens_length[params.data_set]

    logger, base_callbacks = False, list()
    if params.logger:
        comet_config = EasyDict(config['cometml'])
        logger = CometLogger(api_key=comet_config.apikey, project_name=comet_config.projectname,
                             workspace=comet_config.workspace)
        logger.experiment.log_code(folder='src')
        logger.log_hyperparams(params)
        base_callbacks.append(LearningRateMonitor(logging_interval='epoch'))

    df_dataset = pd.read_csv(f'data/{params.data_set}/data.csv')

    if params.fold == 1:
        train_indices, val_indices = train_test_split(list(range(len(df_dataset))), test_size=0.2,
                                                      stratify=df_dataset['label'])
        n_splits = [(train_indices, val_indices)]
    else:
        skf = StratifiedKFold(n_splits=params.fold)
        n_splits = list(skf.split(X=df_dataset['text'], y=df_dataset['label']))

    for fold_id, (train_index, test_index) in enumerate(n_splits):

        model_checkpoint = ModelCheckpoint(
            filepath='checkpoints/{epoch:02d}-{val_loss:.4f}-{val_acc:.4f}_fold_' + str(fold_id),
            save_weights_only=True, save_top_k=10, monitor='val_acc_' + str(fold_id),
            period=1
        )

        callbacks = deepcopy(base_callbacks)
        callbacks.extend([model_checkpoint])

        model_class, tokenizer_class, model_name = model_data[params.model]
        tokenizer = tokenizer_class.from_pretrained(model_name, do_lower_case=True)
        model_backbone = model_class.from_pretrained(model_name, num_labels=2, output_attentions=False,
                                                     output_hidden_states=False)

        train_df, valid_df = df_dataset.iloc[train_index], df_dataset.iloc[test_index]
        train_loader = DataLoader(SentimentDataset(train_df, tokenizer=tokenizer, length=params.length),
                                  num_workers=8, batch_size=params.batch_size, shuffle=True)
        val_loader = DataLoader(SentimentDataset(valid_df, tokenizer=tokenizer, length=params.length),
                                num_workers=8, batch_size=params.batch_size, shuffle=False)

        model = TransformerLitModule(model=model_backbone, tokenizer=tokenizer, lr=params.lr, fold_id=fold_id)

        trainer = Trainer(
            auto_lr_find=params.find_lr,
            logger=logger,
            max_epochs=params.epoch,
            callbacks=callbacks,
            gpus=1,
            deterministic=True,
            fast_dev_run=params.fast_dev_run,
            log_every_n_steps=10
        )

        trainer.fit(model, train_dataloader=train_loader, val_dataloaders=val_loader)

        if params.logger:
            for absolute_path in model_checkpoint.best_k_models.keys():
                logger.experiment.log_model(Path(absolute_path).name, absolute_path)
            if model_checkpoint.best_model_score:
                logger.log_metrics({'best_model_score_' + str(fold_id): model_checkpoint.best_model_score.tolist()})


if __name__ == '__main__':
    train()
