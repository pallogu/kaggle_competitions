import numpy as np
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
from numpy.random import seed
from tensorflow.random import set_seed
import pandas as pd
from tf_agents.environments import tf_py_environment
from tf_agents.trajectories.time_step import TimeStep
import time
import mlflow

seed(42)
set_seed(42)

# ### Environments

from environment import MarketEnv

train = pd.read_csv("../etl/train_dataset_after_pca.csv")
eval_df = pd.read_csv("../etl/val_dataset_after_pca.csv")

# +
# eval_df = eval_df[eval_df["date"] < 420]
train_py_env = MarketEnv(
    trades = train,
    features = ["f_{i}".format(i=i) for i in range(40)] + ["weight"],
    reward_column = "resp",
    weight_column = "weight",
    discount=0.9
)

val_py_env = MarketEnv(
    trades = eval_df,
    features = ["f_{i}".format(i=i) for i in range(40)] + ["weight"],
    reward_column = "resp",
    weight_column = "weight",
    discount=0.9
)

tf_env = tf_py_environment.TFPyEnvironment(train_py_env)
eval_tf_env = tf_py_environment.TFPyEnvironment(val_py_env)

# +
actor_nn_arch = (
    tf_env.time_step_spec().observation.shape[0],
    256,
    128,
    16,
    2
)
actor_model = keras.Sequential([
    layers.Input(shape = actor_nn_arch[0]),
    layers.Dense(
        actor_nn_arch[1],
        activation = "relu",
        kernel_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=1/actor_nn_arch[0], seed=1)
    ),
    layers.Dropout(0.5),
    layers.Dense(
        actor_nn_arch[2],
        activation = "relu",
        kernel_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=1/actor_nn_arch[1], seed=2)
    ),
    layers.Dropout(0.5),
    layers.Dense(
        actor_nn_arch[3],
        activation = "relu",
        kernel_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=1/actor_nn_arch[2], seed=3)
    ),
    layers.Dense(
        actor_nn_arch[4],
        activation = "softmax",
        kernel_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=1/actor_nn_arch[3], seed=4)
    )
])

critic_nn_arch = (
    tf_env.time_step_spec().observation.shape[0],
    256,
    128,
    64,
    1
)

critic_model = keras.Sequential([
    layers.Input(shape = critic_nn_arch[0]),
    layers.Dense(
        critic_nn_arch[1],
        activation = "relu",
        kernel_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=1/critic_nn_arch[0], seed=11)
    ),
    layers.Dropout(0.5),
    layers.Dense(
        critic_nn_arch[2],
        activation = "relu",
        kernel_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=1/critic_nn_arch[1], seed=12)
    ),
    layers.Dropout(0.5),
    layers.Dense(
        critic_nn_arch[3],
        activation = "relu",
        kernel_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=1/critic_nn_arch[2], seed=13)
    ),
    layers.Dense(
        critic_nn_arch[4],
        kernel_initializer = tf.keras.initializers.RandomNormal(mean=0.0, stddev=1/critic_nn_arch[3], seed=14)
    )
])
# -

# ## Hyperparameters

avg_reward_step_size = 1e-3
actor_step_size = 1e-6
critic_step_size = 1e-5
number_of_episodes = 2


def calculate_u_metric(df, model):
    # print("evaluating policy")
  
    actions = np.argmax(model(df[["f_{i}".format(i=i) for i in range(40)] + ["weight"]].values). numpy(), axis=1)
            
    df["action"] = pd.Series(data=actions, index = df.index)
    df["trade_reward"] = df["action"]*df["weight"]*df["resp"]
    df["trade_reward_squared"] = df["trade_reward"]*df["trade_reward"]

    tmp = df.groupby(["date"])[["trade_reward", "trade_reward_squared"]].agg("sum")
        
    sum_of_pi = tmp["trade_reward"].sum()
    sum_of_pi_x_pi = tmp["trade_reward_squared"].sum()
    
    # print("sum of pi: {sum_of_pi}".format(sum_of_pi = sum_of_pi) )
        
    t = sum_of_pi/np.sqrt(sum_of_pi_x_pi) * np.sqrt(250/tmp.shape[0])
    # print("t: {t}".format(t = t) )
    
    u = np.min([np.max([t, 0]), 6]) * sum_of_pi
    # print("u: {u}".format(u = u) )
            
    return t, u


# ## AC Agent

# +
class ACAgent():
    def __init__(self, **kwargs):
        self.actor_model = kwargs.get("actor_model")
        self.critic_model = kwargs.get("critic_model")
        self.avg_reward_step_size = kwargs.get("avg_reward_step_size")
        
        actor_step_size = kwargs.get("actor_step_size")
        critic_step_size = kwargs.get("critic_step_size")
        
        self.actor_optimizer = keras.optimizers.Adam(learning_rate=actor_step_size)
        self.critic_optimizer = keras.optimizers.Adam(learning_rate=critic_step_size)
        
        self.reward = 0
        self.delta = 0
        self.prev_observation = None
        self.prev_action = None
        
    def init(self, time_step):
        observation = time_step.observation

        action = np.random.choice([0, 1])
        self.prev_observation = observation
        self.prev_action = action
                
        return action

    def train(self, time_step):
        observation = time_step.observation
        reward = time_step.reward
        
        self.update_avg_reward(reward)
        self.update_td_error(reward, observation)
        self.update_critic_model(observation)
        self.update_actor_model(observation)
        
        probs = self.actor_model(observation).numpy()[0]

        action = np.random.choice(
            [0, 1],
            p=probs
        )
        
        self.prev_action = action
        self.prev_observation = observation

        return action
        
    def update_avg_reward(self, observation_reward):
        self.reward = (1-self.avg_reward_step_size)*self.reward + self.avg_reward_step_size * observation_reward
        
    def update_td_error(self, observation_reward, observation):        
        self.delta = (
            observation_reward 
            - self.reward
            + self.critic_model(observation).numpy()[0][0]
            - self.critic_model(self.prev_observation).numpy()[0][0]
        )
        
    def update_critic_model(self, observation):
        with tf.GradientTape() as tape:
            grad = [-1 * self.delta * g for g in  tape.gradient(
                self.critic_model(observation),
                self.critic_model.trainable_variables
            )]
            
            self.critic_optimizer.apply_gradients(
                zip(grad, self.critic_model.trainable_variables),
                experimental_aggregate_gradients=False
            )
            
    def update_actor_model(self, observation):
        prev_action = self.prev_action
        with tf.GradientTape() as tape:
            grad = [-1 * self.delta * g for g in tape.gradient(
                tf.math.log(self.actor_model(observation)),
                self.actor_model.trainable_variables
            )]
            
            last_layer_w = grad[-2].numpy()
            last_layer_w[:, prev_action] = 0
            grad[-2] = tf.constant(last_layer_w, dtype=np.float32)
            
            last_layer_b = grad[-1].numpy()
            last_layer_b[prev_action] = 0
            grad[-1] = tf.constant(last_layer_b, dtype=np.float32)

            self.actor_optimizer.apply_gradients(
                zip(grad, self.actor_model.trainable_variables),
                experimental_aggregate_gradients=False
            )   



# +
## Test Cell

np.random.seed(42)


actor_model_test = keras.Sequential([
    layers.Input(shape = 4),
    layers.Dense(
        4,
        activation = "relu",
        kernel_initializer = tf.keras.initializers.Constant(value=1)
    ),
    layers.Dense(
        2,
        activation = "softmax",
        kernel_initializer = tf.keras.initializers.Constant(value=1)
    )
])


critic_model_test = keras.Sequential([
    layers.Input(shape = 4),
    layers.Dense(
        4,
        activation = "relu",
        kernel_initializer = tf.keras.initializers.Constant(value=1)
    ),
    layers.Dense(
        1,
        kernel_initializer = tf.keras.initializers.Constant(value=1)
    )
])

agent_test = ACAgent(
    actor_model=actor_model_test,
    critic_model=critic_model_test,
    avg_reward_step_size=0.1,
    actor_step_size=0.1,
    critic_step_size=0.1
)

agent_test.init(TimeStep(
        step_type = tf.constant([0], dtype=np.int32),
        reward = tf.constant([0], dtype=np.float32),
        discount = tf.constant([1], dtype=np.float32),
        observation = tf.constant(np.array([[1, 1, 1, 1]]), dtype=np.float64),  
    )
)

agent_test.update_avg_reward(1)
assert agent_test.reward == 0.1
agent_test.update_avg_reward(1)
assert agent_test.reward == 0.19
agent_test.update_avg_reward(1)
assert agent_test.reward == 0.271

agent_test.update_td_error(1, tf.constant(np.array([[1, 1, 0, 0]]), dtype=np.float64))
assert agent_test.delta == -7.271000000000001



# +
# %%time

agent = ACAgent(
    actor_model=actor_model,
    critic_model=critic_model,
    avg_reward_step_size=avg_reward_step_size,
    actor_step_size=actor_step_size,
    critic_step_size=critic_step_size
)

def run_experiment():
    with mlflow.start_run():
        
        mlflow.set_tag("agent_type", "sac")
        mlflow.log_param("actor_nn_layers", actor_nn_arch )
        mlflow.log_param("critic_nn_layers", critic_nn_arch)
        mlflow.log_param("avg_reward_step_size", avg_reward_step_size)
        mlflow.log_param("actor_step_size", actor_step_size)
        mlflow.log_param("critic_step_size", critic_step_size)
    
        t = time.localtime()
        for epoch in range(number_of_episodes):
            time_step = tf_env.reset()
            action = agent.init(time_step)
            counter = 0
            while not time_step.is_last():
                time_step = tf_env.step(action)
                action = agent.train(time_step)
                counter += 1

                if counter % 10000 == 0:
                    current_time = time.strftime("%H:%M:%S", t)
                    print(epoch, counter, current_time)
                    t_eval, u_eval = calculate_u_metric(eval_df, actor_model)
                    t_train, u_train = calculate_u_metric(train, actor_model)
                    mlflow.log_metrics({
                        "t_eval": t_eval,
                        "u_eval": u_eval,
                        "t_train": t_train,
                        "u_train": u_train
                    })


run_experiment()
# -
actor_model.trainable_variables()

actor_model(train[:2][["f_{i}".format(i=i) for i in range(40)] + ["weight"]].values). numpy()

actor_model.summary()


