#! /usr/bin/python3
# coding:utf-8

"""
@author:Fang Wang
@date:2020-01-09
@desc:
"""

import time
import requests

import db

proxy_url_new = "https://proxyapi.horocn.com/api/v2/proxies?order_id=XZLN1709793577214599&num=10&format=text&line_separator=win&user_token=ce1accbaf4e5bf38cde9289646afc1b1"


def get_proxy_new():
    r = requests.get(proxy_url_new)
    if r.status_code == 200:
        proxy_list = [x.strip() for x in r.text.split("\n")]
        return proxy_list
    else:
        return


def insert_proxies(proxy_list):
    proxies_data = list()
    for each_proxy in proxy_list:
        host, port = each_proxy.split(":")
        proxies_data.append((host, port, each_proxy, 0))

    # insert data
    ret = db.insert_proxy(proxies_data)
    if ret:
        print("Succeeded to insert {} proxies to database".format(ret))


def crawl_proxies():
    while True:
        try:
            proxy_list = get_proxy_new()
            insert_proxies(proxy_list)
            time.sleep(10)
        except Exception as e:
            print("Failed to crawl proxies due to {}".format(str(e)))
            time.sleep(30)


if __name__ == "__main__":
    crawl_proxies()
