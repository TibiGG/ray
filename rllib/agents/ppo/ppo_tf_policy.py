"""
TensorFlow policy class used for PPO.
"""

import gym
import logging
from typing import Dict, List, Optional, Type, Union

import ray
from ray.rllib.evaluation.episode import Episode
from ray.rllib.evaluation.postprocessing import (
    compute_gae_for_sample_batch,
    Postprocessing,
)
from ray.rllib.models.modelv2 import ModelV2
from ray.rllib.models.tf.tf_action_dist import TFActionDistribution
from ray.rllib.policy.policy import Policy
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.policy.tf_mixins import (
    EntropyCoeffSchedule,
    KLCoeffMixin,
    LearningRateSchedule,
    ValueNetworkMixin,
)
from ray.rllib.policy.tf_policy_template import build_tf_policy
from ray.rllib.utils.deprecation import (
    Deprecated,
    DEPRECATED_VALUE,
    deprecation_warning,
)
from ray.rllib.utils.framework import try_import_tf
from ray.rllib.utils.tf_utils import explained_variance
from ray.rllib.utils.typing import (
    AgentID,
    LocalOptimizer,
    ModelGradients,
    TensorType,
    TrainerConfigDict,
)

tf1, tf, tfv = try_import_tf()

logger = logging.getLogger(__name__)


def ppo_surrogate_loss(
    policy: Policy,
    model: Union[ModelV2, "tf.keras.Model"],
    dist_class: Type[TFActionDistribution],
    train_batch: SampleBatch,
) -> Union[TensorType, List[TensorType]]:
    """Constructs the loss for Proximal Policy Objective.

    Args:
        policy (Policy): The Policy to calculate the loss for.
        model (Union[ModelV2, tf.keras.Model]): The Model to calculate
            the loss for.
        dist_class (Type[ActionDistribution]: The action distr. class.
        train_batch (SampleBatch): The training data.

    Returns:
        Union[TensorType, List[TensorType]]: A single loss tensor or a list
            of loss tensors.
    """
    if isinstance(model, tf.keras.Model):
        logits, state, extra_outs = model(train_batch)
        value_fn_out = extra_outs[SampleBatch.VF_PREDS]
    else:
        logits, state = model(train_batch)
        value_fn_out = model.value_function()

    curr_action_dist = dist_class(logits, model)

    # RNN case: Mask away 0-padded chunks at end of time axis.
    if state:
        # Derive max_seq_len from the data itself, not from the seq_lens
        # tensor. This is in case e.g. seq_lens=[2, 3], but the data is still
        # 0-padded up to T=5 (as it's the case for attention nets).
        B = tf.shape(train_batch[SampleBatch.SEQ_LENS])[0]
        max_seq_len = tf.shape(logits)[0] // B

        mask = tf.sequence_mask(train_batch[SampleBatch.SEQ_LENS], max_seq_len)
        mask = tf.reshape(mask, [-1])

        def reduce_mean_valid(t):
            return tf.reduce_mean(tf.boolean_mask(t, mask))

    # non-RNN case: No masking.
    else:
        mask = None
        reduce_mean_valid = tf.reduce_mean

    prev_action_dist = dist_class(train_batch[SampleBatch.ACTION_DIST_INPUTS], model)

    logp_ratio = tf.exp(
        curr_action_dist.logp(train_batch[SampleBatch.ACTIONS])
        - train_batch[SampleBatch.ACTION_LOGP]
    )

    # Only calculate kl loss if necessary (kl-coeff > 0.0).
    if policy.config["kl_coeff"] > 0.0:
        action_kl = prev_action_dist.kl(curr_action_dist)
        mean_kl_loss = reduce_mean_valid(action_kl)
    else:
        mean_kl_loss = tf.constant(0.0)

    curr_entropy = curr_action_dist.entropy()
    mean_entropy = reduce_mean_valid(curr_entropy)

    surrogate_loss = tf.minimum(
        train_batch[Postprocessing.ADVANTAGES] * logp_ratio,
        train_batch[Postprocessing.ADVANTAGES]
        * tf.clip_by_value(
            logp_ratio, 1 - policy.config["clip_param"], 1 + policy.config["clip_param"]
        ),
    )
    mean_policy_loss = reduce_mean_valid(-surrogate_loss)

    # Compute a value function loss.
    if policy.config["use_critic"]:
        vf_loss = tf.math.square(
            value_fn_out - train_batch[Postprocessing.VALUE_TARGETS]
        )
        vf_loss_clipped = tf.clip_by_value(
            vf_loss,
            0,
            policy.config["vf_clip_param"],
        )
        mean_vf_loss = reduce_mean_valid(vf_loss_clipped)
    # Ignore the value function.
    else:
        vf_loss_clipped = mean_vf_loss = tf.constant(0.0)

    total_loss = reduce_mean_valid(
        -surrogate_loss
        + policy.config["vf_loss_coeff"] * vf_loss_clipped
        - policy.entropy_coeff * curr_entropy
    )
    # Add mean_kl_loss (already processed through `reduce_mean_valid`),
    # if necessary.
    if policy.config["kl_coeff"] > 0.0:
        total_loss += policy.kl_coeff * mean_kl_loss

    # Store stats in policy for stats_fn.
    policy._total_loss = total_loss
    policy._mean_policy_loss = mean_policy_loss
    policy._mean_vf_loss = mean_vf_loss
    policy._mean_entropy = mean_entropy
    # Backward compatibility: Deprecate policy._mean_kl.
    policy._mean_kl_loss = policy._mean_kl = mean_kl_loss
    policy._value_fn_out = value_fn_out

    return total_loss


def kl_and_loss_stats(
    policy: Policy, train_batch: SampleBatch
) -> Dict[str, TensorType]:
    """Stats function for PPO. Returns a dict with important KL and loss stats.

    Args:
        policy (Policy): The Policy to generate stats for.
        train_batch (SampleBatch): The SampleBatch (already) used for training.

    Returns:
        Dict[str, TensorType]: The stats dict.
    """
    return {
        "cur_kl_coeff": tf.cast(policy.kl_coeff, tf.float64),
        "cur_lr": tf.cast(policy.cur_lr, tf.float64),
        "total_loss": policy._total_loss,
        "policy_loss": policy._mean_policy_loss,
        "vf_loss": policy._mean_vf_loss,
        "vf_explained_var": explained_variance(
            train_batch[Postprocessing.VALUE_TARGETS], policy._value_fn_out
        ),
        "kl": policy._mean_kl_loss,
        "entropy": policy._mean_entropy,
        "entropy_coeff": tf.cast(policy.entropy_coeff, tf.float64),
    }


# TODO: (sven) Deprecate once we only allow native keras models.
def vf_preds_fetches(policy: Policy) -> Dict[str, TensorType]:
    """Defines extra fetches per action computation.

    Args:
        policy (Policy): The Policy to perform the extra action fetch on.

    Returns:
        Dict[str, TensorType]: Dict with extra tf fetches to perform per
            action computation.
    """
    # Keras models return values for each call in third return argument
    # (dict).
    if isinstance(policy.model, tf.keras.Model):
        return {}
    # Return value function outputs. VF estimates will hence be added to the
    # SampleBatches produced by the sampler(s) to generate the train batches
    # going into the loss function.
    return {
        SampleBatch.VF_PREDS: policy.model.value_function(),
    }


def compute_and_clip_gradients(
    policy: Policy, optimizer: LocalOptimizer, loss: TensorType
) -> ModelGradients:
    """Gradients computing function (from loss tensor, using local optimizer).

    Args:
        policy (Policy): The Policy object that generated the loss tensor and
            that holds the given local optimizer.
        optimizer (LocalOptimizer): The tf (local) optimizer object to
            calculate the gradients with.
        loss (TensorType): The loss tensor for which gradients should be
            calculated.

    Returns:
        ModelGradients: List of the possibly clipped gradients- and variable
            tuples.
    """
    # Compute the gradients.
    variables = policy.model.trainable_variables
    if isinstance(policy.model, ModelV2):
        variables = variables()
    grads_and_vars = optimizer.compute_gradients(loss, variables)

    # Clip by global norm, if necessary.
    if policy.config["grad_clip"] is not None:
        # Defuse inf gradients (due to super large losses).
        grads = [g for (g, v) in grads_and_vars]
        grads, _ = tf.clip_by_global_norm(grads, policy.config["grad_clip"])
        # If the global_norm is inf -> All grads will be NaN. Stabilize this
        # here by setting them to 0.0. This will simply ignore destructive loss
        # calculations.
        policy.grads = [tf.where(tf.math.is_nan(g), tf.zeros_like(g), g) for g in grads]
        clipped_grads_and_vars = list(zip(policy.grads, variables))
        return clipped_grads_and_vars
    else:
        return grads_and_vars


def setup_config(
    policy: Policy,
    obs_space: gym.spaces.Space,
    action_space: gym.spaces.Space,
    config: TrainerConfigDict,
) -> None:
    """Executed before Policy is "initialized" (at beginning of constructor).

    Args:
        policy (Policy): The Policy object.
        obs_space (gym.spaces.Space): The Policy's observation space.
        action_space (gym.spaces.Space): The Policy's action space.
        config (TrainerConfigDict): The Policy's config.
    """
    # Setting `vf_share_layers` in the top-level config is deprecated.
    # It's confusing as some users might (correctly!) set it in their
    # model config and then won't notice that it's silently overwritten
    # here.
    if config.get("vf_share_layers", DEPRECATED_VALUE) != DEPRECATED_VALUE:
        deprecation_warning(
            old="config[vf_share_layers]",
            new="config[model][vf_share_layers]",
            error=True,
        )
        config["model"]["vf_share_layers"] = config["vf_share_layers"]

    # If vf_share_layers is True, inform about the need to tune vf_loss_coeff.
    if config.get("model", {}).get("vf_share_layers") is True:
        logger.info(
            "`vf_share_layers=True` in your model. "
            "Therefore, remember to tune the value of `vf_loss_coeff`!"
        )


def setup_mixins(
    policy: Policy,
    obs_space: gym.spaces.Space,
    action_space: gym.spaces.Space,
    config: TrainerConfigDict,
) -> None:
    """Call mixin classes' constructors before Policy's loss initialization.

    Args:
        policy (Policy): The Policy object.
        obs_space (gym.spaces.Space): The Policy's observation space.
        action_space (gym.spaces.Space): The Policy's action space.
        config (TrainerConfigDict): The Policy's config.
    """
    ValueNetworkMixin.__init__(policy, config)
    KLCoeffMixin.__init__(policy, config)
    EntropyCoeffSchedule.__init__(
        policy, config["entropy_coeff"], config["entropy_coeff_schedule"]
    )
    LearningRateSchedule.__init__(policy, config["lr"], config["lr_schedule"])


@Deprecated(
    old="rllib.agents.ppo.ppo_tf_policy.postprocess_ppo_gae",
    new="rllib.evaluation.postprocessing.compute_gae_for_sample_batch",
    error=False,
)
def postprocess_ppo_gae(
    policy: Policy,
    sample_batch: SampleBatch,
    other_agent_batches: Optional[Dict[AgentID, SampleBatch]] = None,
    episode: Optional[Episode] = None,
) -> SampleBatch:

    return compute_gae_for_sample_batch(
        policy, sample_batch, other_agent_batches, episode
    )


# Build a child class of `DynamicTFPolicy`, given the custom functions defined
# above.
PPOTFPolicy = build_tf_policy(
    name="PPOTFPolicy",
    loss_fn=ppo_surrogate_loss,
    get_default_config=lambda: ray.rllib.agents.ppo.ppo.DEFAULT_CONFIG,
    postprocess_fn=compute_gae_for_sample_batch,
    stats_fn=kl_and_loss_stats,
    compute_gradients_fn=compute_and_clip_gradients,
    extra_action_out_fn=vf_preds_fetches,
    before_init=setup_config,
    before_loss_init=setup_mixins,
    mixins=[
        LearningRateSchedule,
        EntropyCoeffSchedule,
        KLCoeffMixin,
        ValueNetworkMixin,
    ],
)
