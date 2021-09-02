#! /usr/bin/python3
# coding:utf-8

"""
@author:Fang Wang
@date:2020-01-09
@desc:
"""

import db
import json
import random

from flask import Flask
from flask import request
from logger import logger

app = Flask(__name__)

IN_USE = 1
INVALID = 2
SUCCESS = 3


def get_proxy_from_db():
    sql = "select * from proxy where status in (0, 3) order by update_time desc limit 10"
    result = db.query_by_sql(sql)
    if result:
        return random.choice(result)[2]
    else:
        return ""


def update_proxy_status(status, proxy_str):
    ret = db.update_proxy(status, proxy_str)
    if ret:
        logger.info("Succeeded to update status for proxy {}".format(proxy_str))


@app.route("/proxy")
def get_proxy():
    proxy_str = get_proxy_from_db()
    logger.info("Got a proxy {}".format(proxy_str))
    update_proxy_status(IN_USE, proxy_str)
    return proxy_str


@app.route("/update")
def update_proxy():
    proxy = request.args.get("proxy")
    status = int(request.args.get("status"))
    if status == IN_USE or status == INVALID or status == SUCCESS:
        update_proxy_status(status, proxy)
    else:
        logger.error("Skipped to update status due to invalid status code")

    return json.dumps({"result": "success"})


@app.route("/")
def hello_world():
    return "<p>Hello, World!</p>"


if __name__ == "__main__":
    app.run(port=5000, debug=False)
