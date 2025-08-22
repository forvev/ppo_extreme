"""Common networks used in RL.

This file contains nn.Module definitions for common networks used in RL. It is divided into three sets:

1) Common Networks: MLP
2) Common RL Networks:
    For discrete action spaces: DiscreteCritic is a Q-function
    For continuous action spaces: Critic, ValueCritic, and Policy provide the Q-function, value function, and policy respectively.
    For ensembling: ensemblize() provides a wrapper for creating ensembles of networks (e.g. for min-Q / double-Q)
3) Meta Networks for vision tasks:
    WithEncoder: Combines a fully connected network with an encoder network (encoder may come from jaxrl_m.vision)
    ActorCritic: Same as WithEncoder, but for possibly many different networks (e.g. actor, critic, value)
"""

from jaxrl_m.typing import *

import flax.linen as nn
import jax.numpy as jnp
import jax
import distrax



###############################
#
#  Common Networks
#
###############################


# ###FOR TANH
def tanh_init(scale: Optional[float] = jnp.sqrt(2.0)):
    return nn.initializers.orthogonal(scale)


def relu_init(scale: Optional[float] = 1.0):
    return nn.initializers.variance_scaling(scale, "fan_avg", "uniform")


class MLP(nn.Module):
    
    hidden_dims: Sequence[int]
    activations: Callable[[jnp.ndarray], jnp.ndarray]     
    use_layer_norm: bool
    activate_final: bool
    use_bias : bool = True
    

    @nn.compact
    def __call__(self, x: jnp.ndarray, train=False) -> jnp.ndarray:
        for i, size in enumerate(self.hidden_dims):
            
            kernel_init = tanh_init() if self.activations == nn.tanh else relu_init()

            x = nn.Dense(size, kernel_init=kernel_init,use_bias=self.use_bias)(x)

            if i + 1 < len(self.hidden_dims) or self.activate_final:
                if self.use_layer_norm:
                    x = nn.LayerNorm()(x)
                x = self.activations(x)
        return x




class Critic(nn.Module):
    hidden_dims: Sequence[int]
    use_layer_norm: bool
    activations: Callable[[jnp.ndarray], jnp.ndarray]
    scale_final: Optional[float] = None

    @nn.compact
    def __call__(self, observations: jnp.ndarray, actions: jnp.ndarray,
                *args,**kwargs) -> jnp.ndarray:
        inputs = jnp.concatenate([observations, actions], -1)
        critic = MLP((*self.hidden_dims, 2),activations=self.activations,
                     use_layer_norm=self.use_layer_norm)(inputs,*args, **kwargs)
        
        return critic[:,0] , critic[:,1]
    
    

class OriginalCritic(nn.Module):
    hidden_dims: Sequence[int]
    use_layer_norm: bool
    activations: Callable[[jnp.ndarray], jnp.ndarray]
    scale_final: Optional[float] = None
    

    @nn.compact
    def __call__(self, observations: jnp.ndarray, actions: jnp.ndarray,
                *args,**kwargs) -> jnp.ndarray:
        inputs = jnp.concatenate([observations, actions], -1)
        intermediate = MLP(self.hidden_dims,activate_final=True,
                     use_layer_norm=self.use_layer_norm,activations=self.activations)(inputs,*args, **kwargs)
        
        self.sow('intermediates', 'features', intermediate)

        kernel_init = tanh_init() if self.activations == nn.tanh else relu_init()
        Q = nn.Dense(1, kernel_init=kernel_init)(intermediate)
        
        return jnp.squeeze(Q, -1)
    
    
class OriginalV(nn.Module):
    hidden_dims: Sequence[int]
    use_layer_norm: bool
    activations: Callable[[jnp.ndarray], jnp.ndarray]
    scale_final: Optional[float] = None

    @nn.compact
    def __call__(self, observations: jnp.ndarray,*args,**kwargs) -> jnp.ndarray:
        
        intermediate = MLP(self.hidden_dims,activate_final=True,activations=self.activations,
                     use_layer_norm=self.use_layer_norm)(observations,*args, **kwargs)
        
        self.sow('intermediates', 'features', intermediate)

        kernel_init = tanh_init() if self.activations == nn.tanh else relu_init()
        Q = nn.Dense(1, kernel_init=kernel_init)(intermediate)
        
        return jnp.squeeze(Q, -1)


def ensemblize(cls, num_qs, out_axes=0, **kwargs):
    """
    Useful for making ensembles of Q functions (e.g. double Q in SAC).

    Usage:

        critic_def = ensemblize(Critic, 2)(hidden_dims=hidden_dims)

    """
    split_rngs = kwargs.pop("split_rngs", {})
    return nn.vmap(
        cls,
        variable_axes={"params": 0},
        split_rngs={**split_rngs, "params": True},
        in_axes=None,
        out_axes=out_axes,
        axis_size=num_qs,
        **kwargs
    )


 
    
class Policy(nn.Module):
    hidden_dims: Sequence[int]
    action_dim: int
   
    use_layer_norm : bool
    activations: Callable[[jnp.ndarray], jnp.ndarray]
    tanh_squash_distribution: bool 
    final_fc_init_scale: float = 1e-2
    log_std_min: Optional[float] = -5
    log_std_max: Optional[float] = 2
    state_dependent_std: bool = True
    use_bias : bool = True
    min_std : float = 0.05

    

    @nn.compact
    def __call__(
        self, observations: jnp.ndarray, 
        temperature: float = 1.0,
    ) -> distrax.Distribution:
        outputs = MLP(
            self.hidden_dims,
            activate_final=True,
            use_layer_norm=self.use_layer_norm,
            activations=self.activations,
            use_bias= self.use_bias,
        )(observations)

        kernel_init = tanh_init if self.activations == nn.tanh else relu_init

        means = nn.Dense(
            self.action_dim, kernel_init=kernel_init(self.final_fc_init_scale),use_bias=self.use_bias,name="means"
        )(outputs)
        if self.state_dependent_std:
            log_stds = nn.Dense(
                self.action_dim, kernel_init=kernel_init(self.final_fc_init_scale),use_bias=self.use_bias,name="log_stds"
            )(outputs)
        else:
            log_stds = self.param("log_stds", jax.nn.initializers.constant(-4.6), (self.action_dim,))

        log_stds = jnp.clip(log_stds, self.log_std_min, self.log_std_max)

        distribution = distrax.MultivariateNormalDiag(
            loc=means, scale_diag=jnp.exp(log_stds) * temperature
        )
        
            
        if self.tanh_squash_distribution:
            
            distribution = distrax.Transformed(distribution, distrax.Tanh())

        return distribution


class TransformedWithMode(distrax.Transformed):
    def mode(self) -> jnp.ndarray:
        return self.bijector.forward(self.distribution.mode())


