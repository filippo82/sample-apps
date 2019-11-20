import pandas as pd
import numpy as np
from random import sample, seed
from math import floor
import os

import tensorflow as tf
import tensorflow_ranking as tfr

_GROUP_SIZE = 1  # pointwise scoring
_NUM_TRAIN_STEPS = 10 * 1000
_SAVE_CHECKPOINT = 10
losses = [
    tfr.losses.RankingLossKey.SIGMOID_CROSS_ENTROPY_LOSS,
    tfr.losses.RankingLossKey.APPROX_MRR_LOSS,
    tfr.losses.RankingLossKey.SOFTMAX_LOSS,
]
#learning_rates = [0.001, 0.01, 0.1]
learning_rates = [0.1, 0.5, 1]
DATA_FOLDER = "../../data"
DATA_FILE_PATH = "../../data/training_data_match_random_collect_rank_features_99_random_samples.csv"
NUM_DOCS_PER_QUERY = 100

def data_generator(
    dataset: pd.DataFrame, features, label, queries, num_docs, batch_size, num_epochs=1
):
    queries = sample(queries, len(queries))
    batches = []
    batch = []
    for _ in range(num_epochs):
        for qid in queries:
            batch.append(qid)
            if len(batch) == batch_size:
                batches.append(batch)
                batch = []

    for batch in batches:
        y = []
        x = []
        for qid in batch:
            rows = dataset[dataset.qid == qid]
            features_qid = rows[features].values
            labels_qid = rows[label].values
            docs_deviation = num_docs - features_qid.shape[0]
            if docs_deviation > 0:
                # add docs_deviation filled with zeros for features and -1 for labels
                features_qid = np.append(
                    features_qid, np.zeros((docs_deviation, len(features))), axis=0
                )
                labels_qid = np.append(
                    labels_qid, np.full((docs_deviation,), -1), axis=0
                )
            elif docs_deviation < 0:
                # reduce docs_deviation
                features_qid = features_qid[:num_docs, :]
                labels_qid = labels_qid[:num_docs]
            x.append(features_qid.tolist())
            y.append(labels_qid.tolist())
        yield {"x_raw": np.array(x, dtype=np.float32)}, np.array(y, dtype=np.float32)


def transform_fn(features, mode):
    context_features = {}
    group_features = {"x": features["x_raw"]}

    return context_features, group_features


def score_fn(context_features, group_features, mode, params, config):
    """Defines the network to score a group of documents."""
    # input_layer = tf.keras.layers.Flatten(group_features["x"])
    input_layer = tf.compat.v1.layers.flatten(group_features["x"])
    # logits = tf.keras.layers.Dense(input_layer, units=_GROUP_SIZE)
    # logits = tf.compat.v1.layers.dense(group_features["x"], units=_GROUP_SIZE)
    #logits = tf.compat.v1.layers.dense(input_layer, units=_GROUP_SIZE, kernel_initializer=tf.constant_initializer([-0.1, -0.1]))
    logits = tf.compat.v1.layers.dense(input_layer, units=_GROUP_SIZE)
    return logits

def score_nn_fn(context_features, group_features, mode, params, config):
    """Defines the network to score a group of documents."""
    input_layer = tf.compat.v1.layers.flatten(group_features["x"])
    hidden_layer = tf.compat.v1.layers.dense(input_layer, units=16)
    logits = tf.compat.v1.layers.dense(hidden_layer, units=_GROUP_SIZE)
    return logits

def eval_metric_fns():
    metric_fns = {
        "metric/mrr": tfr.metrics.make_ranking_metric_fn(
            tfr.metrics.RankingMetricKey.MRR
        )
    }
    metric_fns.update(
        {
            "metric/ndcg@%d"
            % topn: tfr.metrics.make_ranking_metric_fn(
                tfr.metrics.RankingMetricKey.NDCG, topn=topn
            )
            for topn in [1, 3, 5]
        }
    )

    return metric_fns


def train_and_eval_fn(
    dataset,
    features,
    label,
    train_queries,
    eval_queries,
    loss,
    score_function,
    learning_rate,
    model_dir,
):
    """Train and eval function used by `tf.estimator.train_and_evaluate`."""

    loss_fn = tfr.losses.make_loss_fn(loss)

    optimizer = tf.compat.v1.train.AdagradOptimizer(learning_rate=learning_rate)

    def _train_op_fn(loss):
        """Defines train op used in ranking head."""
        update_ops = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)
        minimize_op = optimizer.minimize(
            loss=loss, global_step=tf.compat.v1.train.get_global_step()
        )
        train_op = tf.group([update_ops, minimize_op])
        return train_op

    ranking_head = tfr.head.create_ranking_head(
        loss_fn=loss_fn, eval_metric_fns=eval_metric_fns(), train_op_fn=_train_op_fn
    )

    model_fn = tfr.model.make_groupwise_ranking_fn(
        group_score_fn=score_function,
        transform_fn=transform_fn,
        group_size=_GROUP_SIZE,
        ranking_head=ranking_head,
    )

    run_config = tf.estimator.RunConfig(save_checkpoints_steps=_SAVE_CHECKPOINT)
    ranker = tf.estimator.Estimator(
        model_fn=model_fn, model_dir=model_dir, config=run_config
    )

    seed(536)
    data_gen_train = data_generator(
        dataset=dataset,
        features=features,
        label=label,
        queries=train_queries,
        num_docs=NUM_DOCS_PER_QUERY,
        batch_size=16,
        num_epochs=5,
    )

    def train_input_fn():
        return next(data_gen_train)

    data_gen_eval = data_generator(
        dataset=dataset,
        features=features,
        label=label,
        queries=eval_queries,
        num_docs=NUM_DOCS_PER_QUERY,
        batch_size=16,
        num_epochs=5,
    )

    def eval_input_fn():
        return next(data_gen_eval)

    train_spec = tf.estimator.TrainSpec(
        input_fn=train_input_fn, max_steps=_NUM_TRAIN_STEPS
    )
    eval_spec = tf.estimator.EvalSpec(
        name="eval", input_fn=eval_input_fn, throttle_secs=0, steps=_SAVE_CHECKPOINT
    )
    return ranker, train_spec, eval_spec


if __name__ == "__main__":

    #
    # Read csv file with data
    #
    full_data = pd.read_csv(
        DATA_FILE_PATH,
        usecols=["qid", "docid", "relevant", "bm25(title)", "bm25(body)", "nativeRank(title)", "nativeRank(body)"],
    )

    unique_queries = set(full_data.qid)
    train_queries = set(sample(unique_queries, floor(len(unique_queries) / 2)))
    eval_queries = unique_queries - set(train_queries)

    for loss in losses:
        for lr in learning_rates:
            model_dir = os.path.join(
                DATA_FOLDER, "tf_ranking_100_docs_nn_4_features_" + str(loss) + "_" + str(lr)
            )

            ranker, train_spec, eval_spec = train_and_eval_fn(
                dataset=full_data,
                features=["bm25(title)", "bm25(body)", "nativeRank(title)", "nativeRank(body)"],
                label="relevant",
                train_queries=train_queries,
                eval_queries=eval_queries,
                loss=loss,
                score_function=score_nn_fn,
                learning_rate=lr,
                model_dir=model_dir,
            )
            tf.estimator.train_and_evaluate(ranker, train_spec, eval_spec)
            print(ranker.get_variable_value("group_score/dense/bias"))
            print(ranker.get_variable_value("group_score/dense/kernel"))
            with open(os.path.join(model_dir, "parameters.txt"), "w") as f:
                f.write(str(ranker.get_variable_value("group_score/dense/bias")))
                f.write(str(ranker.get_variable_value("group_score/dense/kernel")))
