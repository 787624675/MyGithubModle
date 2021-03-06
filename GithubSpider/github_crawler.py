#!/usr/bin/env python
# -*- coding: utf-8 -*-
import re
import sys
import time
import math
import urllib
import json, requests
import dateutil.parser
import clean_utils as cu
import concurrent.futures
import mysql_option as mysql
from bs4 import BeautifulSoup
from multiprocessing import cpu_count
from datetime import datetime, timedelta


# update your token here, https://github.com/settings/tokens
TOKEN = open('token_key', 'r').read()
# query limit is 30 times per hour
MAX_PAGE = 11
# max is 100 record per page
ITEM_PER_PAGE = 100
# get start from specific time
START_FROM_TIME = '\"2016-07-21T02:59:59Z .. 2016-07-21T03:59:59Z\"'
# end time
END_TO_TIME = '2016-06-30T23:59:59Z'
# for a specific time,result is muilt-page,get start from specific page
SRART_FROM_PAGE = 2
# max core we can use
MAX_WORKER = (cpu_count())*2
# files you want for a project
TYPE = ['build.gradle', 'pom.xml']
# project update date
UPADATE_DATE = '2012-01-01T19:01:12Z'
# thread pool handler
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKER)


# remove inside readme file and decrease the number of dependency
def _get_real_file_url(urls):
    # sort dependency url according to url length,if it is too long
    # means it is a very deep sub-part of a project
    urls.sort(lambda x, y: cmp(len(str(x).split('/')), len(str(x).split('/'))))
    # only get the top 10 dependency files
    if (len(urls) > 20):
        urls = urls[:20]
    return urls


# count down 4 hours for a new search
def get_new_time(time_period, need_sub):
    time_period_list = str(time_period).replace('\"', '').split(' .. ')
    if (need_sub):
        start_time = dateutil.parser.parse(time_period_list[0])
        end_time = time_period_list[0]
        start_time = (start_time - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
        return '\"' + start_time + ' .. ' + end_time + '\"', start_time
    else:
        start_time = dateutil.parser.parse(time_period_list[0]).isoformat().replace('+00:00', 'Z')
        return time_period, start_time


# extract information of single project
def get_information(item):
    dict_item = dict(item)
    project_name = dict_item.get("name")
    project_url = dict_item.get("url")
    git_url = dict_item.get("git_url")

    # do another query for a specific project and modify GET header,query limits is 5000 per hour
    head = {"Accept": "application/vnd.github.mercy-preview+json", "Authorization": "token " + TOKEN}
    try:
        res = dict(json.loads(requests.get(project_url, headers=head).content))
        origin_id = res.get('id')
        if (len(res.get('topics')) > 0):
            topics = '#'.join(list(res.get('topics')))
        else:
            topics = ''
        description = res.get('description')
        full_name = res.get('full_name')

        dependency_urls = []
        min_readme_lenghth = 100000
        min_readme_path = ''
        get_file_url = project_url + '/git/trees/master?recursive=1'
        file_result = json.loads(requests.get(get_file_url, headers=head).content)
    except Exception, e:
        print e

    if (dict(file_result).has_key('tree')):
        for r in dict(file_result).get('tree'):
            r = dict(r)
            if (r.get('type') == 'blob'):
                temp_path = str(r.get('path'))
                if (('README' in temp_path) and (min_readme_lenghth > len(temp_path))):
                    min_readme_path = temp_path
                    min_readme_lenghth = len(temp_path)
                else:
                    for type_choose in TYPE:
                        if (type_choose in temp_path):
                            blob_url = "https://raw.githubusercontent.com/" + full_name + '/master/' + r.get('path')
                            dependency_urls.append(blob_url)
    file_urls = _get_real_file_url(dependency_urls)
    readme_url = "https://raw.githubusercontent.com/" + full_name + '/master/' + min_readme_path
    return project_name, git_url, topics, description, origin_id, file_urls, readme_url


def _extract_info_from_file(url):
    dependency = []
    try:
        page = urllib.urlopen(url)
        text = page.read()
        # README.md
        if url.find('README') != -1:
            readme = cu.extract_markdown(text)
            dependency.append(readme)
        # build.gradle
        elif url.find(TYPE[0]) != -1:
            index = text.find("dependencies {")
            text = text[index:]
            index = text.find("}")
            text = text[:index]
            p = re.compile(r'\'(.*:.*:.*)\'\n')
            for dep in p.findall(text):
                dependency.append(dep)
        # pom.xml
        elif url.find(TYPE[1]) != -1:
            soup = BeautifulSoup(text, "lxml")
            dep = soup.findAll(name="dependency")
            for i in range(len(dep)):
                temp = dep[i].contents
                if (len(temp) == 7):
                    dependency.append(temp[1].text + ":" + temp[3].text + ":" + temp[5].text)
                else:
                    dependency.append(temp[1].text + ":" + temp[3].text + ":" + '')
    except Exception as e:
        print(e)
    return dependency


# extract readme and dependency from url
def extract_info_from_file(urls, readme_url):
    # format is groupId:artifactId:version
    # eg, com.android.tools.build:gradle:1.2.3, split by :
    dependency = []
    dependency_group = []
    dependency_name = []
    dependency_version = []
    readme = ''
    if ('README' in readme_url):
        readme = _extract_info_from_file(readme_url)[0]

    # single thread extraction
    # for url in urls:
    #     dependence = _extract_info_from_file(url)
    #     dependency.extend(dependence)

    # muilti threads extraction
    for url, dependence in zip(urls, executor.map(_extract_info_from_file, urls)):
        dependency.extend(dependence)

    for depend in dependency:
        infos = str(depend).split(":")
        dependency_group.append(infos[0])
        dependency_name.append(infos[1])
        dependency_version.append(infos[2])

    return readme, dependency, '#'.join(dependency_group), '#'.join(dependency_name), '#'.join(dependency_version)


# main function for whole project
def crawl_url(need_insert_database):
    id = 0
    if (need_insert_database):
        mysql_handler = mysql.Mysql(mysql.USER, mysql.PWD, mysql.DB_NAME, mysql.TABLE_NAME)

    is_restart = True
    time_period, start_time = get_new_time(START_FROM_TIME, False)
    while (start_time > END_TO_TIME):
        if (is_restart):
            is_restart = False
            page = SRART_FROM_PAGE
        else:
            time_period, start_time = get_new_time(start_time, True)
            page = 1
        max_page = MAX_PAGE
        while (page < max_page+1):
            url = "https://api.github.com/search/repositories?q=created:" + time_period + "+language:Java&per_page=" + str(
                ITEM_PER_PAGE) + "&page=" + str(page) + "&access_token=" + TOKEN
            request_result = requests.get(url)
            print 'API limit time is ' + str(request_result.headers.get('X-RateLimit-Remaining'))
            res = json.loads(request_result.content)
            max_page = min(int(math.ceil(float(res.get('total_count')) / ITEM_PER_PAGE)), MAX_PAGE)
            items = dict(res).get("items")
            # every page has several project
            if(len(items)==0):
                continue
            for item in items:
                if (item.get('updated_at') > UPADATE_DATE):
                    try:
                        file_urls = ''
                        project_name, git_url, topics, description, origin_id, file_urls, readme_url = get_information(item)
                        if (not len(file_urls) == 0):
                            # extract info from files
                            readme, dependency, group, name, version = extract_info_from_file(file_urls, readme_url)
                            if (not (description == '' and readme == '')):
                                description = cu.clean(description)
                                readme = cu.clean(readme)
                                if (need_insert_database):
                                    id += 1
                                    data = (str(origin_id), str(project_name), str(description), readme, name, group, version,
                                    git_url, readme_url, str(item.get('created_at')),str(item.get('updated_at')),topics)
                                    print(str(id) + '-------' + str(page) + '-----' + str(
                                        max_page) + '-----' + project_name + '-----------' + time_period + '------' + topics)

                                    mysql_handler.insert(data)
                                    mysql_handler.connection.commit()

                                else:
                                    print(project_name)
                    except Exception as e:
                            print(e)
            page += 1


if __name__ == "__main__":
    crawl_url(True)
    # print START_FROM_TIME
    # print get_new_time(START_FROM_TIME,False)
    executor.shutdown()
