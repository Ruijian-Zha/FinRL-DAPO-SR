from __future__ import annotations

from typing import List

import gymnasium as gym
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from gymnasium import spaces
from gymnasium.utils import seeding
from stable_baselines3.common.vec_env import DummyVecEnv

matplotlib.use("Agg")

# from stable_baselines3.common.logger import Logger, KVWriter, CSVOutputFormat


class StockTradingEnv(gym.Env):
    """A stock trading environment for OpenAI gym"""

    metadata = {"render.modes": ["human"]}

    def __init__(
        self,
        df: pd.DataFrame,
        stock_dim: int,
        hmax: int,
        initial_amount: int,
        num_stock_shares: list[int],
        buy_cost_pct: list[float],
        sell_cost_pct: list[float],
        reward_scaling: float,
        state_space: int,
        action_space: int,
        tech_indicator_list: list[str],
        turbulence_threshold=None,
        risk_indicator_col="turbulence",
        llm_sentiment_col="llm_sentiment", #added llm_sentiment
        llm_risk_col="llm_risk",
        make_plots: bool = False,
        print_verbosity=10,
        day=0,
        initial=True,
        previous_state=[],
        model_name="",
        mode="",
        iteration="",
    ):
        self.day = day
        self.df = df
        self.stock_dim = stock_dim
        self.hmax = hmax
        self.num_stock_shares = num_stock_shares
        self.initial_amount = initial_amount  # get the initial cash
        self.buy_cost_pct = buy_cost_pct
        self.sell_cost_pct = sell_cost_pct
        self.reward_scaling = reward_scaling
        self.state_space = state_space
        self.action_space = action_space
        self.tech_indicator_list = tech_indicator_list
        self.action_space = spaces.Box(low=-1, high=1, shape=(self.action_space,))
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(self.state_space,)
        )
        self.data = self.df.loc[self.day, :]
        self.terminal = False
        self.make_plots = make_plots
        self.print_verbosity = print_verbosity
        self.turbulence_threshold = turbulence_threshold
        self.risk_indicator_col = risk_indicator_col
        self.llm_sentiment_col=llm_sentiment_col
        self.llm_risk_col=llm_risk_col
        self.initial = initial
        self.previous_state = previous_state
        self.model_name = model_name
        self.mode = mode
        self.iteration = iteration
        # initalize state
        self.state = self._initiate_state()

        # initialize reward
        self.reward = 0
        self.turbulence = 0
        self.cost = 0
        self.trades = 0
        self.episode = 0
        # memorize all the total balance change
        self.asset_memory = [
            self.initial_amount
            + np.sum(
                np.array(self.num_stock_shares)
                * np.array(self.state[1 : 1 + self.stock_dim])
            )
        ]  # the initial total asset is calculated by cash + sum (num_share_stock_i * price_stock_i)
        self.rewards_memory = []
        self.actions_memory = []
        self.state_memory = (
            []
        )  # we need sometimes to preserve the state in the middle of trading process
        self.date_memory = [self._get_date()]
        #         self.logger = Logger('results',[CSVOutputFormat])
        # self.reset()
        self.seed()

    def _sell_stock(self, index, action):
        def _do_sell_normal():
            if (
                self.state[index + 2 * self.stock_dim + 1] != True
            ):  # check if the stock is able to sell, for simlicity we just add it in techical index
                # if self.state[index + 1] > 0: # if we use price<0 to denote a stock is unable to trade in that day, the total asset calculation may be wrong for the price is unreasonable
                # Sell only if the price is > 0 (no missing data in this particular date)
                # perform sell action based on the sign of the action
                if self.state[index + self.stock_dim + 1] > 0:
                    # Sell only if current asset is > 0
                    sell_num_shares = min(
                        abs(action), self.state[index + self.stock_dim + 1]
                    )
                    sell_amount = (
                        self.state[index + 1]
                        * sell_num_shares
                        * (1 - self.sell_cost_pct[index])
                    )
                    # update balance
                    self.state[0] += sell_amount

                    self.state[index + self.stock_dim + 1] -= sell_num_shares
                    self.cost += (
                        self.state[index + 1]
                        * sell_num_shares
                        * self.sell_cost_pct[index]
                    )
                    self.trades += 1
                else:
                    sell_num_shares = 0
            else:
                sell_num_shares = 0

            return sell_num_shares

        # perform sell action based on the sign of the action
        if self.turbulence_threshold is not None:
            if self.turbulence >= self.turbulence_threshold:
                if self.state[index + 1] > 0:
                    # Sell only if the price is > 0 (no missing data in this particular date)
                    # if turbulence goes over threshold, just clear out all positions
                    if self.state[index + self.stock_dim + 1] > 0:
                        # Sell only if current asset is > 0
                        sell_num_shares = self.state[index + self.stock_dim + 1]
                        sell_amount = (
                            self.state[index + 1]
                            * sell_num_shares
                            * (1 - self.sell_cost_pct[index])
                        )
                        # update balance
                        self.state[0] += sell_amount
                        self.state[index + self.stock_dim + 1] = 0
                        self.cost += (
                            self.state[index + 1]
                            * sell_num_shares
                            * self.sell_cost_pct[index]
                        )
                        self.trades += 1
                    else:
                        sell_num_shares = 0
                else:
                    sell_num_shares = 0
            else:
                sell_num_shares = _do_sell_normal()
        else:
            sell_num_shares = _do_sell_normal()

        return sell_num_shares

    def _buy_stock(self, index, action):
        def _do_buy():
            if (
                self.state[index + 2 * self.stock_dim + 1] != True
            ):  # check if the stock is able to buy
                # if self.state[index + 1] >0:
                # Buy only if the price is > 0 (no missing data in this particular date)
                available_amount = self.state[0] // (
                    self.state[index + 1] * (1 + self.buy_cost_pct[index])
                )  # when buying stocks, we should consider the cost of trading when calculating available_amount, or we may be have cash<0
                # print('available_amount:{}'.format(available_amount))

                # update balance
                buy_num_shares = min(available_amount, action)
                buy_amount = (
                    self.state[index + 1]
                    * buy_num_shares
                    * (1 + self.buy_cost_pct[index])
                )
                self.state[0] -= buy_amount

                self.state[index + self.stock_dim + 1] += buy_num_shares

                self.cost += (
                    self.state[index + 1] * buy_num_shares * self.buy_cost_pct[index]
                )
                self.trades += 1
            else:
                buy_num_shares = 0

            return buy_num_shares

        # perform buy action based on the sign of the action
        if self.turbulence_threshold is None:
            buy_num_shares = _do_buy()
        else:
            if self.turbulence < self.turbulence_threshold:
                buy_num_shares = _do_buy()
            else:
                buy_num_shares = 0
                pass

        return buy_num_shares

    def _make_plot(self):
        plt.plot(self.asset_memory, "r")
        plt.savefig(f"results/account_value_trade_{self.episode}.png")
        plt.close()

    def step(self, actions):
        self.terminal = self.day >= len(self.df.index.unique()) - 1
        if self.terminal:
            # Terminal state handling
            if self.make_plots:
                self._make_plot()
            end_total_asset = self.state[0] + sum(
                np.array(self.state[1 : (self.stock_dim + 1)])
                * np.array(self.state[(self.stock_dim + 1) : (self.stock_dim * 2 + 1)])
            )
            df_total_value = pd.DataFrame(self.asset_memory)
            tot_reward = (
                self.state[0]
                + sum(
                    np.array(self.state[1 : (self.stock_dim + 1)])
                    * np.array(
                        self.state[(self.stock_dim + 1) : (self.stock_dim * 2 + 1)]
                    )
                )
                - self.asset_memory[0]
            )
            df_total_value.columns = ["account_value"]
            df_total_value["date"] = self.date_memory
            df_total_value["daily_return"] = df_total_value["account_value"].pct_change(1)
            
            sharpe = 0
            if df_total_value["daily_return"].std() != 0:
                sharpe = (
                    (252**0.5)
                    * df_total_value["daily_return"].mean()
                    / df_total_value["daily_return"].std()
                )
            
            df_rewards = pd.DataFrame(self.rewards_memory)
            df_rewards.columns = ["account_rewards"]
            df_rewards["date"] = self.date_memory[:-1]
            
            if self.episode % self.print_verbosity == 0:
                print(f"day: {self.day}, episode: {self.episode}")
                print(f"begin_total_asset: {self.asset_memory[0]:0.2f}")
                print(f"end_total_asset: {end_total_asset:0.2f}")
                print(f"total_reward: {tot_reward:0.2f}")
                print(f"total_cost: {self.cost:0.2f}")
                print(f"total_trades: {self.trades}")
                if df_total_value["daily_return"].std() != 0:
                    print(f"Sharpe: {sharpe:0.3f}")
                print("=================================")

            if (self.model_name != "") and (self.mode != ""):
                df_actions = self.save_action_memory()
                df_actions.to_csv(
                    "results/actions_{}_{}_{}.csv".format(
                        self.mode, self.model_name, self.iteration
                    )
                )
                df_total_value.to_csv(
                    "results/account_value_{}_{}_{}.csv".format(
                        self.mode, self.model_name, self.iteration
                    ),
                    index=False,
                )
                df_rewards.to_csv(
                    "results/account_rewards_{}_{}_{}.csv".format(
                        self.mode, self.model_name, self.iteration
                    ),
                    index=False,
                )
                plt.plot(self.asset_memory, "r")
                plt.savefig(
                    "results/account_value_{}_{}_{}.png".format(
                        self.mode, self.model_name, self.iteration
                    )
                )
                plt.close()

            return self.state, self.reward, self.terminal, False, {}

        else:
            # Handle the current trading day
            try:
                # Safely get LLM sentiments and risks with proper error handling
                if len(self.df.tic.unique()) > 1:
                    # For multiple stocks
                    llm_sentiments = np.array(self.data[self.llm_sentiment_col].values)
                    llm_risks = np.array(self.data[self.llm_risk_col].values)
                else:
                    # For single stock - convert to array to maintain consistent shape
                    llm_sentiments = np.array([self.data[self.llm_sentiment_col]])
                    llm_risks = np.array([self.data[self.llm_risk_col]])
                
                # Convert actions to numpy array if not already
                actions = np.array(actions).flatten()
                
                # Ensure actions and sentiment/risk arrays match in length
                if len(llm_sentiments) != len(actions):
                    print(f"Warning: Sentiment array length ({len(llm_sentiments)}) doesn't match actions length ({len(actions)})")
                    # Adjust to smaller size for safety
                    min_len = min(len(llm_sentiments), len(actions))
                    llm_sentiments = llm_sentiments[:min_len]
                    llm_risks = llm_risks[:min_len]
                    actions = actions[:min_len]
                    
                    # If we need to extend sentiment/risk arrays:
                    if len(llm_sentiments) < len(actions):
                        llm_sentiments = np.pad(llm_sentiments, (0, len(actions) - len(llm_sentiments)), 'constant', constant_values=3)
                        llm_risks = np.pad(llm_risks, (0, len(actions) - len(llm_risks)), 'constant', constant_values=3)
                
                # Create masks for action types
                buy_mask = (actions > 0)
                sell_mask = (actions < 0)

                # Create masks based on LLM sentiments
                strong_sell_mask = np.zeros_like(actions, dtype=bool)
                moderate_sell_mask = np.zeros_like(actions, dtype=bool)
                hold_mask = np.zeros_like(actions, dtype=bool)
                moderate_buy_mask = np.zeros_like(actions, dtype=bool)
                strong_buy_mask = np.zeros_like(actions, dtype=bool)
                
                # Fill in masks safely (ensuring bounds are respected)
                for i in range(min(len(llm_sentiments), len(actions))):
                    if i < len(llm_sentiments):
                        sentiment = llm_sentiments[i]
                        if sentiment == 1:
                            strong_sell_mask[i] = True
                        elif sentiment == 2:
                            moderate_sell_mask[i] = True
                        elif sentiment == 3:
                            hold_mask[i] = True
                        elif sentiment == 4:
                            moderate_buy_mask[i] = True
                        elif sentiment == 5:
                            strong_buy_mask[i] = True

                # Adjust actions based on combined conditions (safely)
                # Reduce mismatched strong actions
                for i in range(len(actions)):
                    if (strong_sell_mask[i] and buy_mask[i]) or (strong_buy_mask[i] and sell_mask[i]):
                        actions[i] *= 0.9
                    # Reduce mismatched moderate actions
                    elif (moderate_sell_mask[i] and buy_mask[i]) or (moderate_buy_mask[i] and sell_mask[i]):
                        actions[i] *= 0.95
                    # Amplify matched strong actions
                    elif (strong_sell_mask[i] and sell_mask[i]) or (strong_buy_mask[i] and buy_mask[i]):
                        actions[i] *= 1.1
                    # Amplify matched moderate actions
                    elif (moderate_sell_mask[i] and sell_mask[i]) or (moderate_buy_mask[i] and buy_mask[i]):
                        actions[i] *= 1.05

                # Scale actions according to hmax
                actions = actions * self.hmax
                actions = actions.astype(int)
                
                # Handle turbulence
                if self.turbulence_threshold is not None:
                    if self.turbulence >= self.turbulence_threshold:
                        actions = np.array([-self.hmax] * self.stock_dim)
                
                # Get current total asset value
                begin_total_asset = self.state[0] + sum(
                    np.array(self.state[1 : (self.stock_dim + 1)])
                    * np.array(self.state[(self.stock_dim + 1) : (self.stock_dim * 2 + 1)])
                )

                # Sort actions for processing sell orders first
                argsort_actions = np.argsort(actions)
                sell_index = argsort_actions[: np.where(actions < 0)[0].shape[0]]
                buy_index = argsort_actions[::-1][: np.where(actions > 0)[0].shape[0]]

                # Process sell actions
                for index in sell_index:
                    if index < self.stock_dim:  # Safety check to prevent index errors
                        actions[index] = self._sell_stock(index, actions[index]) * (-1)

                # Process buy actions
                for index in buy_index:
                    if index < self.stock_dim:  # Safety check to prevent index errors
                        actions[index] = self._buy_stock(index, actions[index])

                self.actions_memory.append(actions)

                # Update state
                self.day += 1
                if self.day >= len(self.df.index.unique()):
                    # Edge case: reached end of data
                    self.terminal = True
                    return self.state, self.reward, self.terminal, False, {}
                    
                self.data = self.df.loc[self.day, :]
                
                # Update turbulence if needed
                if self.turbulence_threshold is not None:
                    try:
                        if len(self.df.tic.unique()) == 1:
                            self.turbulence = self.data[self.risk_indicator_col]
                        elif len(self.df.tic.unique()) > 1:
                            if self.risk_indicator_col in self.data.columns:
                                self.turbulence = self.data[self.risk_indicator_col].values[0]
                            else:
                                # Default to no turbulence if column not found
                                self.turbulence = 0
                    except Exception as e:
                        print(f"Error updating turbulence: {e}")
                        self.turbulence = 0
                
                # Update state
                self.state = self._update_state()

                # Calculate reward
                end_total_asset = self.state[0] + sum(
                    np.array(self.state[1 : (self.stock_dim + 1)])
                    * np.array(self.state[(self.stock_dim + 1) : (self.stock_dim * 2 + 1)])
                )
                self.asset_memory.append(end_total_asset)
                self.date_memory.append(self._get_date())
                self.reward = end_total_asset - begin_total_asset
                self.rewards_memory.append(self.reward)
                self.reward = self.reward * self.reward_scaling
                self.state_memory.append(self.state)

                return self.state, self.reward, self.terminal, False, {}
                
            except Exception as e:
                print(f"Error in step method: {e}")
                import traceback
                traceback.print_exc()
                
                # Return current state with no changes as fallback
                # Prevent termination to allow debugging
                return self.state, 0, False, False, {"error": str(e)}

    def reset(
        self,
        *,
        seed=None,
        options=None,
    ):
        # initiate state
        self.day = 0
        self.data = self.df.loc[self.day, :]
        self.state = self._initiate_state()

        if self.initial:
            self.asset_memory = [
                self.initial_amount
                + np.sum(
                    np.array(self.num_stock_shares)
                    * np.array(self.state[1 : 1 + self.stock_dim])
                )
            ]
        else:
            previous_total_asset = self.previous_state[0] + sum(
                np.array(self.state[1 : (self.stock_dim + 1)])
                * np.array(
                    self.previous_state[(self.stock_dim + 1) : (self.stock_dim * 2 + 1)]
                )
            )
            self.asset_memory = [previous_total_asset]

        self.turbulence = 0
        self.cost = 0
        self.trades = 0
        self.terminal = False
        # self.iteration=self.iteration
        self.rewards_memory = []
        self.actions_memory = []
        self.date_memory = [self._get_date()]

        self.episode += 1

        return self.state, {}

    def render(self, mode="human", close=False):
        return self.state

    def _initiate_state(self):
        if self.initial:
            # For Initial State
            if len(self.df.tic.unique()) > 1:
                # for multiple stock
         #       print("the type of self data is:  " type(self.data.close))
            #    print("the llm sentiment is " + str(self.data[self.llm_sentiment_col].tolist()))

#                print(' the closing vals are ' + str(self.data.close))

                state = ([self.initial_amount]+ self.data.close.values.tolist()+ self.num_stock_shares+ sum(
                        (self.data[tech].values.tolist() for tech in self.tech_indicator_list),[],)
                    +  self.data[self.llm_sentiment_col].values.tolist()  #add llm sentiment
                    +  self.data[self.llm_risk_col].values.tolist()  #add llm sentiment
                )  # append initial stocks_share to initial state, instead of all zero
            else:
                # for single stock
                state = (
                    [self.initial_amount]
                    + [self.data.close]
                    + [0] * self.stock_dim
                    + sum(([self.data[tech]] for tech in self.tech_indicator_list), [])
                    + [self.data[self.llm_sentiment_col]]
                    + [self.data[self.llm_risk_col]]
                )
        else:
            # Using Previous State
            if len(self.df.tic.unique()) > 1:
                # for multiple stock
                state = (
                    [self.previous_state[0]]
                    + self.data.close.values.tolist()
                    + self.previous_state[
                        (self.stock_dim + 1) : (self.stock_dim * 2 + 1)
                    ]
                    + sum(
                        (
                            self.data[tech].values.tolist()
                            for tech in self.tech_indicator_list
                        ),
                        [],
                    )
                )
            else:
                # for single stock
                state = (
                    [self.previous_state[0]]
                    + [self.data.close]
                    + self.previous_state[
                        (self.stock_dim + 1) : (self.stock_dim * 2 + 1)
                    ]
                    + sum(([self.data[tech]] for tech in self.tech_indicator_list), [])
                )

        return state

    def _update_state(self):
        if len(self.df.tic.unique()) > 1:
            # for multiple stock
            state = (
                [self.state[0]]
                + self.data.close.values.tolist()
                + list(self.state[(self.stock_dim + 1) : (self.stock_dim * 2 + 1)])
                + sum(
                    (
                        self.data[tech].values.tolist()
                        for tech in self.tech_indicator_list
                    ),
                    [],
                )
                + self.data[self.llm_sentiment_col].values.tolist() # add LLM sentiment
                + self.data[self.llm_risk_col].values.tolist() # add LLM risk
            )

        else:
            # for single stock
            state = (
                [self.state[0]]
                + [self.data.close]
                + list(self.state[(self.stock_dim + 1) : (self.stock_dim * 2 + 1)])
                + sum(([self.data[tech]] for tech in self.tech_indicator_list), [])
                + [self.data[self.llm_sentiment_col]] #add LLM sentiment
                + [self.data[self.llm_risk_col]] #add LLM risk

            )

        return state

    def _get_date(self):
        if len(self.df.tic.unique()) > 1:
            date = self.data.date.unique()[0]
        else:
            date = self.data.date
        return date

    # add save_state_memory to preserve state in the trading process
    def save_state_memory(self):
        if len(self.df.tic.unique()) > 1:
            # date and close price length must match actions length
            date_list = self.date_memory[:-1]
            df_date = pd.DataFrame(date_list)
            df_date.columns = ["date"]

            state_list = self.state_memory
            df_states = pd.DataFrame(
                state_list,
                columns=[
                    "cash",
                    "Bitcoin_price",
                    "Gold_price",
                    "Bitcoin_num",
                    "Gold_num",
                    "Bitcoin_Disable",
                    "Gold_Disable",
                ],
            )
            df_states.index = df_date.date
            # df_actions = pd.DataFrame({'date':date_list,'actions':action_list})
        else:
            date_list = self.date_memory[:-1]
            state_list = self.state_memory
            df_states = pd.DataFrame({"date": date_list, "states": state_list})
        # print(df_states)
        return df_states

    def save_asset_memory(self):
        date_list = self.date_memory
        asset_list = self.asset_memory
        # print(len(date_list))
        # print(len(asset_list))
        df_account_value = pd.DataFrame(
            {"date": date_list, "account_value": asset_list}
        )
        return df_account_value

    def save_action_memory(self):
        if len(self.df.tic.unique()) > 1:
            # date and close price length must match actions length
            date_list = self.date_memory[:-1]
            df_date = pd.DataFrame(date_list)
            df_date.columns = ["date"]

            action_list = self.actions_memory
            df_actions = pd.DataFrame(action_list)
            df_actions.columns = self.data.tic.values
            df_actions.index = df_date.date
            # df_actions = pd.DataFrame({'date':date_list,'actions':action_list})
        else:
            date_list = self.date_memory[:-1]
            action_list = self.actions_memory
            df_actions = pd.DataFrame({"date": date_list, "actions": action_list})
        return df_actions

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def get_sb_env(self):
        e = DummyVecEnv([lambda: self])
        obs = e.reset()
        return e, obs