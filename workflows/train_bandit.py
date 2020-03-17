"""
Usage:
    python -m workflows.train_bandit --params_path configs/bandit.json \
        --model_path trained_models/test.pkl
"""

import argparse
import json
import time
from typing import Dict, List, NoReturn, Tuple

from google.cloud import bigquery
from google.oauth2 import service_account
import numpy as np
import pandas as pd
from sklearn.utils import shuffle
from sklearn import preprocessing
from skorch import NeuralNetRegressor
import torch
from torch.nn.utils.rnn import pad_sequence

from data_reader.bandit_reader import BigQueryReader
from ml.models.embed_dnn import EmbedDnn
from ml.serving.predictor import BanditPredictor
from ml.serving.model_io import write_predictor_to_disk
from utils.utils import get_logger, fancy_print, read_config

logger = get_logger(__name__)


def get_experiment_specific_params():
    """Place holder function that will call into gradient-app
    and get specific training configs. TODO: fill this in with a request."""

    return {
        "experiment_id": "test-experiment-height-prediction",
        "decisions_ds_start": "2020-03-09",
        "decisions_ds_end": "2020-03-12",
        "rewards_ds_end": "2020-03-13",
        "features": {
            "country": {
                "type": "C",
                "possible_values": [1, 2, 3, 4, 5],
                "product_set_id": "1",
                "use_dense": False,
            },
            "year": {"type": "N", "possible_values": None, "product_set_id": None},
            "decision": {
                "type": "C",
                "possible_values": [0, 1],
                "product_set_id": None,
            },
        },
        "reward_function": {"height": 1},
        "product_sets": {
            "1": {
                "ids": [1, 2, 3, 4, 5],
                "dense": {
                    1: [0, 10.0],
                    2: [1, 8.5],
                    3: [1, 7.5],
                    4: [2, 11.5],
                    5: [2, 10.5],
                },
                "features": [
                    {"name": "region", "type": "C", "possible_values": [0, 1, 2]},
                    {"name": "avg_shoe_size_m", "type": "N", "possible_values": None},
                ],
            }
        },
    }


def get_preprocess_feature_order(features_spec: Dict) -> List[str]:
    """Get order that featuers should be fed into the preprocessor. This will
    need to match bandit-app. For consistency, we will enforce the following
    ordering logic:

    Split features into float_features (i.e. N, C )& id_features (i.e. P)
        - 1st order by feature type ["N", "C"] & ["P"]
        - 2nd order by alphabetical on feature_name a->z
    """
    N, C, P = [], [], []
    for feature_name, meta in features_spec.items():
        if meta["type"] == "N":
            N.append(feature_name)
        elif meta["type"] == "C":
            C.append(feature_name)
        elif meta["type"] == "P":
            P.append(feature_name)
        else:
            raise Exception(f"Feature type {meta['type']} not supported.")

    N.sort()
    C.sort()
    P.sort()

    float_feature_order = N + C
    id_feature_order = P
    return float_feature_order, id_feature_order


def preprocess_feature(
    feature_name: str, feature_type: str, values: np.array
) -> Tuple[pd.DataFrame, int]:
    assert feature_type in ("N", "C")

    # convert to scikit learn expected format
    values = values.reshape(-1, 1)

    if feature_type == "N":
        # standard scaler seems to be right choice for numeric features
        # in regression problems
        # http://rajeshmahajan.com/standard-scaler-v-min-max-scaler-machine-learning/
        preprocessor = preprocessing.StandardScaler()
        values = preprocessor.fit_transform(values)
        df = pd.DataFrame(values.squeeze(), columns=[feature_name])
    elif feature_type == "C":
        preprocessor = preprocessing.OneHotEncoder(sparse=False)
        values = preprocessor.fit_transform(values)
        preprocessor.col_names = [
            feature_name + "_" + str(i) for i in preprocessor.categories_[0]
        ]
        df = pd.DataFrame(values.squeeze(), columns=preprocessor.col_names)
    else:
        raise Exception(f"Feature type {feature_type} not supported.")

    return df, preprocessor


def preprocess_data(
    raw_data: pd.DataFrame, params: pd.DataFrame, shuffle_data=True
) -> Tuple[pd.DataFrame, pd.DataFrame]:

    # start by randomizing the data upfront
    if shuffle_data:
        raw_data = shuffle(raw_data)

    # load the json string into json objects and expand into columns &
    # fill metrics NaN's with 0's.
    X = pd.json_normalize(raw_data["context"].apply(json.loads))
    X["decision"] = raw_data["decision"]
    y = pd.json_normalize(
        raw_data["metrics"].apply(lambda x: json.loads(x) if x else {})
    )
    y = y.fillna(0)

    # construct the reward scalar using a linear combination
    X["reward"] = pd.Series(0, index=range(len(y)))
    for metrics_name, series in y.iteritems():
        X["reward"] += series * params["reward_function"][metrics_name]

    # drop NaN value rows for now TODO: think about if imputing these rows
    # may be better than just dropping them.
    X.dropna(inplace=True)

    reward_df = X["reward"]
    float_feature_df = pd.DataFrame()
    id_list_feature_df = pd.DataFrame()

    # order in which to preprocess features
    float_feature_order, id_feature_order = get_preprocess_feature_order(
        params["features"]
    )
    # final features names after preprocessing & expansion of categoricals
    final_float_feature_order, final_id_feature_order = [], []

    transforms = {}
    for feature_name in float_feature_order:
        meta = params["features"][feature_name]
        df, preprocessor = preprocess_feature(
            feature_name, meta["type"], X[feature_name].values
        )
        final_float_feature_order.extend(df.columns)
        float_feature_df = pd.concat([float_feature_df, df], axis=1)
        transforms[feature_name] = preprocessor

    for feature_name in id_feature_order:
        meta = params["features"][feature_name]
        products_set_id = meta["product_set_id"]
        product_set_meta = params["product_sets"][products_set_id]

        if meta["type"] == "P":
            # if dense is true then convert ID's into their dense features
            if meta["use_dense"] is True and "dense" in product_set_meta:
                dense = np.array(
                    [product_set_meta["dense"][i] for i in X[feature_name].values]
                )
                for idx, feature in enumerate(product_set_meta["features"]):
                    vals = dense[:, idx]
                    df, preprocessor = preprocess_feature(
                        feature["name"], feature["type"], vals
                    )
                    final_float_feature_order.extend(df.columns)
                    float_feature_df = pd.concat([float_feature_df, df], axis=1)
                    transforms[feature_name] = preprocessor
            else:
                # sparse id list features aren't preprocessed, instead they use an
                # embedding table which is built into the pytorch model
                final_id_feature_order.append(feature_name)
                id_list_feature_df[feature_name] = pd.Series(X[feature_name].values)
                transforms[feature_name] = params["features"][feature_name][
                    "product_set_id"
                ]
        else:
            raise (f"Feature type {feature_name} not supported.")

    return {
        "y": reward_df,
        "X_float": float_feature_df,
        "X_id_list": id_list_feature_df,
        "transforms": transforms,
        "float_feature_order": float_feature_order,
        "id_feature_order": id_feature_order,
        "final_float_feature_order": final_float_feature_order,
        "final_id_feature_order": final_id_feature_order,
    }


def build_pytorch_net(
    feature_specs,
    product_sets,
    float_feature_order,
    layers,
    id_feature_order,
    activations,
    input_dim,
    output_dim=1,
):
    """Build PyTorch model that will be fed into skorch training."""
    layers[0], layers[-1] = input_dim, output_dim
    return EmbedDnn(
        layers,
        activations,
        feature_specs=feature_specs,
        product_sets=product_sets,
        float_feature_order=float_feature_order,
        id_feature_order=id_feature_order,
    )


def data_to_pytorch(
    data: Dict, features: Dict, product_sets: Dict[str, List]
) -> Tuple[Dict, torch.tensor]:
    X = {}

    if len(data["X_float"]) > 0:
        X["X_float"] = torch.tensor(data["X_float"].values, dtype=torch.float32)

    X_id_list, X_id_list_idxs = None, None

    # hack needed due to skorch not handling objects in .fit() besides
    # a dict of lists or tensors (dict of dicts not supported.)
    id_list_pad_idxs = []
    pad_idx = 0

    for _, series in data["X_id_list"].iteritems():
        # wrap decisions in lists
        if series.dtype == int:
            series = series.apply(lambda x: [x])

        pre_pad = [torch.tensor(i, dtype=torch.long) for i in series]
        post_pad = pad_sequence(pre_pad, batch_first=True, padding_value=0)
        idx_offset = post_pad.shape[1]
        id_list_pad_idxs.extend([pad_idx, pad_idx + idx_offset])
        pad_idx += idx_offset

        if X_id_list is not None:
            X_id_list = torch.cat((X_id_list, post_pad), dim=1)
        else:
            X_id_list = post_pad

    # skorch requires all inputs to .fit() to have the same length so we
    # need to repeat our id_list_pad_idxs list.
    if X_id_list is not None:
        X["X_id_list"] = X_id_list
        X["X_id_list_idxs"] = torch.tensor([id_list_pad_idxs for i in X_id_list])

    y = torch.tensor(data["y"].values, dtype=torch.float32).unsqueeze(dim=1)

    return X, y


def num_float_dim(data):
    return len(data["X_float"].columns)


def fit_custom_pytorch_module_w_skorch(module, X, y, hyperparams):
    """Fit a custom PyTorch module using Skorch."""

    skorch_net = NeuralNetRegressor(
        module=module,
        optimizer=torch.optim.Adam,
        lr=hyperparams["learning_rate"],
        optimizer__weight_decay=hyperparams["l2_decay"],
        max_epochs=hyperparams["max_epochs"],
        batch_size=hyperparams["batch_size"],
        iterator_train__shuffle=True,
    )

    skorch_net.fit(X, y)
    return skorch_net


def train(shared_params: Dict, model_path: str = None):
    logger.info("Getting experiment config from banditml.com...")
    experiment_specific_params = get_experiment_specific_params()
    logger.info(f"Got experiment specific params: {experiment_specific_params}")

    logger.info("Initializing data reader...")
    data_reader = BigQueryReader(
        credential_path=shared_params["data_reader"]["credential_path"],
        decisions_table_name=shared_params["data_reader"]["decisions_table_name"],
        rewards_table_name=shared_params["data_reader"]["rewards_table_name"],
        decisions_ds_start=experiment_specific_params["decisions_ds_start"],
        decisions_ds_end=experiment_specific_params["decisions_ds_end"],
        rewards_ds_end=experiment_specific_params["rewards_ds_end"],
        experiment_id=experiment_specific_params["experiment_id"],
    )

    raw_data = data_reader.get_training_data()
    logger.info(f"Got {len(raw_data)} rows of training data.")
    logger.info(raw_data.head())

    data = preprocess_data(raw_data, experiment_specific_params)

    # convert data into format that pytorch module expects
    X, y = data_to_pytorch(
        data,
        experiment_specific_params["features"],
        experiment_specific_params["product_sets"],
    )

    pytorch_net = build_pytorch_net(
        feature_specs=experiment_specific_params["features"],
        product_sets=experiment_specific_params["product_sets"],
        float_feature_order=data["final_float_feature_order"],
        id_feature_order=data["final_id_feature_order"],
        layers=shared_params["model"]["layers"],
        activations=shared_params["model"]["activations"],
        input_dim=num_float_dim(data),
    )
    logger.info(f"Initialized model: {pytorch_net}")

    logger.info(f"Starting training: {shared_params['max_epochs']} epochs")
    skorch_net = fit_custom_pytorch_module_w_skorch(
        module=pytorch_net, X=X, y=y, hyperparams=shared_params
    )

    if model_path is not None:
        predictor = BanditPredictor(
            experiment_specific_params=experiment_specific_params,
            float_feature_order=data["float_feature_order"],
            id_feature_order=data["id_feature_order"],
            transforms=data["transforms"],
            net=skorch_net,
        )
        write_predictor_to_disk(predictor, path=model_path)


def main(args):
    start = time.time()
    fancy_print("Starting workflow", color="green", size=70)
    wf_params = read_config(args.params_path)
    logger.info("Using parameters: {}".format(wf_params))
    train(wf_params, args.model_path)
    logger.info("Workflow completed successfully.")
    logger.info(f"Took {time.time() - start} seconds to complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--params_path", required=True, type=str)
    parser.add_argument("--model_path", required=False, type=str)
    args = parser.parse_args()
    main(args)
