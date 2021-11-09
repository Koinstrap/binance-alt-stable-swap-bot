#!python3
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
import math
import time
import os
import json
import configparser
from logging import Handler, Formatter
import datetime
import requests
import random
from tinydb import TinyDB, Query

# Config consts
CFG_FL_NAME = 'user.cfg'
USER_CFG_SECTION = 'binance_user_config'

# Init config
config = configparser.ConfigParser()
if not os.path.exists(CFG_FL_NAME):
    print('No configuration file (user.cfg) found! See README.')
    exit()
config.read(CFG_FL_NAME)

# Logger setup
logger = logging.getLogger('crypto_trader_logger')
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
fh = logging.FileHandler('crypto_trading.log')
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)
logger.addHandler(fh)

# logging to console
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(formatter)
logger.addHandler(ch)

# Telegram bot
TELEGRAM_CHAT_ID = config.get(USER_CFG_SECTION, 'botChatID')
TELEGRAM_TOKEN = config.get(USER_CFG_SECTION, 'botToken')

class RequestsHandler(Handler):
    def emit(self, record):
        log_entry = self.format(record)
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': log_entry,
            'parse_mode': 'HTML'
        }
        return requests.post("https://api.telegram.org/bot{token}/sendMessage".format(token=TELEGRAM_TOKEN),data=payload).content

class LogstashFormatter(Formatter):
    def __init__(self):
        super(LogstashFormatter, self).__init__()

    def format(self, record):
        t = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

        if isinstance(record.msg, dict):
            message = "<i>{datetime}</i>".format(datetime=t)

            for key in record.msg:
                message = message + ("<pre>\n{title}: <strong>{value}</strong></pre>".format(title=key, value=record.msg[key]))

            return message
        else:
            return "<i>{datetime}</i><pre>\n{message}</pre>".format(message=record.msg, datetime=t)

# logging to Telegram if token exists
if TELEGRAM_TOKEN:
    th = RequestsHandler()
    formatter = LogstashFormatter()
    th.setFormatter(formatter)
    logger.addHandler(th)

logger.info('Started')

supported_coin_list = []
stable_coins = ["USDT", "BUSD"]

# Get supported coin list from supported_coin_list file
with open('supported_coin_list') as f:
    supported_coin_list = f.read().upper().splitlines()

# Init config
config = configparser.ConfigParser()
if not os.path.exists(CFG_FL_NAME):
    print('No configuration file (user.cfg) found! See README.')
    exit()
config.read(CFG_FL_NAME)

db = TinyDB('db.json')

transactions = db.table('transactions_traded')

def retry(howmany):
    def tryIt(func):
        def f(*args, **kwargs):
            time.sleep(1)
            attempts = 0
            while attempts < howmany:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    print("Failed to Buy/Sell. Trying Again.")
                    if attempts == 0:
                        logger.info(e)
                        attempts += 1
        return f
    return tryIt


def get_market_ticker_price(client, ticker_symbol):
    '''
    Get ticker price of a specific coin
    '''
    for ticker in client.get_symbol_ticker():
        if ticker[u'symbol'] == ticker_symbol:
            return float(ticker[u'price'])
    return None


def get_currency_balance(client, currency_symbol):
    '''
    Get balance of a specific coin
    '''
    for currency_balance in client.get_account()[u'balances']:
        if currency_balance[u'asset'] == currency_symbol:
            return float(currency_balance[u'free'])
    return None

@retry(20)
def sell_alt(client, alt_symbol, crypto_symbol):
    '''
    Sell altcoin
    '''
    ticks = {}
    for filt in client.get_symbol_info(alt_symbol + crypto_symbol)['filters']:
        if filt['filterType'] == 'LOT_SIZE':
            ticks[alt_symbol] = filt['stepSize'].find('1') - 2
            break

    order_quantity = (math.floor(get_currency_balance(client, alt_symbol) *
                                 10**ticks[alt_symbol])/float(10**ticks[alt_symbol]))
    logger.info('Selling {0} of {1}'.format(order_quantity, alt_symbol))

    bal = get_currency_balance(client, alt_symbol)
    logger.info('Balance is {0}'.format(bal))
    order = None
    while order is None:
        order = client.order_market_sell(
            symbol=alt_symbol + crypto_symbol,
            quantity=(order_quantity)
        )

    logger.info('order')
    logger.info(order)

    # Binance server can take some time to save the order
    logger.info("Waiting for Binance")
    time.sleep(5)
    order_recorded = False
    stat = None
    while not order_recorded:
        try:
            time.sleep(3)
            stat = client.get_order(symbol=alt_symbol + crypto_symbol, orderId=order[u'orderId'])
            order_recorded = True
        except BinanceAPIException as e:
            logger.info(e)
            time.sleep(10)
        except Exception as e:
            logger.info("Unexpected Error: {0}".format(e))

    logger.info(stat)
    while stat[u'status'] != 'FILLED':
        logger.info(stat)
        try:
            stat = client.get_order(
                symbol=alt_symbol+crypto_symbol, orderId=order[u'orderId'])
            time.sleep(1)
        except BinanceAPIException as e:
            logger.info(e)
            time.sleep(2)
        except Exception as e:
            logger.info("Unexpected Error: {0}".format(e))

    newbal = get_currency_balance(client, alt_symbol)
    while(newbal >= bal):
        newbal = get_currency_balance(client, alt_symbol)

    logger.info('Sold {0}'.format(alt_symbol))

    return order

def get_24_hours_deposit_history_for_all_coins(client):
    '''
    Get deposit history for all coins
    '''

    deposit_history = client.get_deposit_history(status = 1, startTime = int(time.time() - 86400 * 60) * 1000, endTime = int(time.time()) * 1000)

    filtered_deposit_history = []

    for deposit in deposit_history:
        if deposit['insertTime'] <= ((time.time() * 86400)):
            filtered_deposit_history.append(deposit)

    return filtered_deposit_history


def scout(client, transaction_fee=0.001, multiplier=5):
    '''
        Get deposit history
        Filter only transaction after 24 hours
    '''

    filtered_deposit_history = get_24_hours_deposit_history_for_all_coins(client)
    wallets_balance = client.get_account()[u'balances']

    '''
        Trade all deposit if value is available in balance
    '''
    
    for deposit in filtered_deposit_history:
        for balance in wallets_balance:
            if balance['asset'] == deposit['coin'] and float(balance['free']) >= float(deposit['amount']) and deposit['coin'] not in stable_coins:
                logger.info('Found {0} in balance'.format(deposit['coin']))
                # sell_alt(client, deposit['coin'], 'USDT')
                break


def main():
    api_key = config.get(USER_CFG_SECTION, 'api_key')
    api_secret_key = config.get(USER_CFG_SECTION, 'api_secret_key')

    client = Client(api_key, api_secret_key)

    while True:
        try:
            time.sleep(5)
            scout(client)
        except Exception as e:
            logger.info('Error while scouting...\n{}\n'.format(e))


if __name__ == "__main__":
    main()
