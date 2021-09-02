#! /usr/bin/python3
# coding:utf-8

"""
@author:Fang Wang
@date:2020-01-09
@desc:
"""

import logging.config

# set format for log
FORMAT = "%(asctime)-15s %(threadName)s %(filename)s:%(lineno)d %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

logger = logging.getLogger('crawlLog')
logger.setLevel(logging.INFO)


