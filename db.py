#! /usr/bin/env python3
# coding=utf-8

"""
@author:Fang Wang
@date:2019-12-20
@desc:
"""

import configparser
import datetime
import logging

import mysql.connector

config = configparser.RawConfigParser()
config.read("./default.cfg")

SQL_HOST = config.get("DATABASE", "SQL_HOST")
SQL_USER = config.get("DATABASE", "SQL_USER")
SQL_PWD = config.get("DATABASE", "SQL_PWD")
DB_NAME = config.get("DATABASE", "DB_NAME")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s:%(levelname)s:%(message)s"
    )


def get_connection(db_name=DB_NAME):
    conn = mysql.connector.connect(host=SQL_HOST, user=SQL_USER, passwd=SQL_PWD,
                                   db=db_name, charset="utf8")
    return conn


def execute_sql(sql, args=None):
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, args)
        conn.commit()
        ret = cur.rowcount
    except Exception as e:
        logging.error("ExecuteSQL error: %s" % str(e))
        return False
    finally:
        cur.close()
        conn.close()

    return ret


def execute_sqls(sql, args=None):
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.executemany(sql, args)
        conn.commit()
        ret = cur.rowcount
    except Exception as e:
        print("ExecuteSQL error: %s" % str(e))
        return False
    finally:
        cur.close()
        conn.close()

    return ret


def query_by_sql(sql, args=None):
    results = []
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql, args)
        rs = cur.fetchall()
        for row in rs:
            results.append(row)
    except Exception as e:
        logging.error("QueryBySQL error: %s" % str(e))
        return None
    finally:
        cur.close()
        conn.close()

    return results


def insert_proxy(args):
    sql = """insert ignore into `proxy` (`host`, `port`, `proxy_str`, `status`) VALUES (%s, %s, %s, %s)"""
    ret = execute_sqls(sql, args)

    return ret


def update_proxy(status=0, proxy_str=""):
    sql = "update `proxy` set `status` = {} where `proxy_str` = '{}'"
    return execute_sql(sql.format(status, proxy_str))


if __name__ == "__main__":
    pass
