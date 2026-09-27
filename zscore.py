import yfinance as yf
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def calc_weights(investment):
    return investment/np.sum(investment)

def calc_returns(capital, hist_data):
    return 100*(hist_data - capital)/capital

def weighted_returns(returns, weights):
    return np.average(a=returns, weights=weights, axis=1)

def stats(weighted_returns):
    return np.mean(weighted_returns), np.std(weighted_returns)

def zscore(current, mean, std):
    return (current - mean)/std

capital = [300, 347, 350]
weights = calc_weights(capital)
portfolio = yf.Tickers(["KSI", "GOOG", "TSLA"])
hist_data = portfolio.history(period="21d")
hist_data = hist_data.drop(["Dividends", "High", "Low", "Open", "Stock Splits", "Volume"], axis=1)
hist_data = hist_data.drop(hist_data.index[-1])
current_data = portfolio.history(period="1d").drop(["Dividends", "High", "Low", "Open", "Stock Splits", "Volume"], axis=1)
print(current_data)
# print(hist_data)
# print(hist_data.shape)

calculated_returns = calc_returns(capital, hist_data)
w_returns = weighted_returns(calculated_returns, weights)
mean, std = stats(w_returns)

print(zscore(weighted_returns(calc_returns(capital, current_data), weights), mean, std))

# going to have to redo the zscore implementation.
#  we monitor %1D Z-score closely : Number of Standard Deviation %1D price change is above 20D moving average
# Z Score = ((Value - Interval Average (Value)) / StdDev(Value)).
