# encoding=utf8
import datetime
from distutils.version import StrictVersion
import hashlib
import os.path
import random
from seesaw.config import realize, NumberConfigValue
from seesaw.externalprocess import ExternalProcess
from seesaw.item import ItemInterpolation, ItemValue
from seesaw.task import SimpleTask, LimitConcurrent
from seesaw.tracker import GetItemFromTracker, PrepareStatsForTracker, \
    UploadWithTracker, SendDoneToTracker
import shutil
import socket
import subprocess
import sys
import time
import string
import re
import random

try:
    import warcio
    from warcio.archiveiterator import ArchiveIterator
    from warcio.warcwriter import WARCWriter
except:
    raise Exception("Please install warc with 'sudo pip install warcio --upgrade'.")

import seesaw
from seesaw.externalprocess import WgetDownload
from seesaw.pipeline import Pipeline
from seesaw.project import Project
from seesaw.util import find_executable

from tornado import httpclient


# check the seesaw version
if StrictVersion(seesaw.__version__) < StrictVersion('0.8.5'):
    raise Exception('This pipeline needs seesaw version 0.8.5 or higher.')


###########################################################################
# Find a useful Wget+Lua executable.
#
# WGET_LUA will be set to the first path that
# 1. does not crash with --version, and
# 2. prints the required version string
WGET_LUA = find_executable(
    'Wget+Lua',
    ['GNU Wget 1.14.lua.20130523-9a5c', 'GNU Wget 1.14.lua.20160530-955376b'],
    [
        './wget-lua',
        './wget-lua-warrior',
        './wget-lua-local',
        '../wget-lua',
        '../../wget-lua',
        '/home/warrior/wget-lua',
        '/usr/bin/wget-lua'
    ]
)

if not WGET_LUA:
    raise Exception('No usable Wget+Lua found.')


###########################################################################
# The version number of this pipeline definition.
#
# Update this each time you make a non-cosmetic change.
# It will be added to the WARC files and reported to the tracker.
VERSION = '20190622.01'
with open('user-agents', 'r') as f:
    USER_AGENT = random.choice(f.read().splitlines()).strip()
TRACKER_ID = 'sketch'
TRACKER_HOST = 'tracker.archiveteam.org'


###########################################################################
# Some helpfull functions
#
#
#
def get_hash(filename):
    with open(filename, 'rb') as in_file:
        return hashlib.sha1(in_file.read()).hexdigest()


def stats_id_function(item):
    # NEW for 2014! Some accountability hashes and stats.
    d = {
        'pipeline_hash': PIPELINE_SHA1,
        'lua_hash': LUA_SHA1,
        'python_version': sys.version,
    }

    return d


###########################################################################
# This section defines project-specific tasks.
#
# Simple tasks (tasks that do not need any concurrency) are based on the
# SimpleTask class and have a process(item) method that is called for
# each item.
CWD = os.getcwd()
PIPELINE_SHA1 = get_hash(os.path.join(CWD, 'pipeline.py'))
LUA_SHA1 = get_hash(os.path.join(CWD, 'sketch.lua'))


class CheckIP(SimpleTask):
    """
    This task checks the IP address to ensure the user is not behind a proxy or
    firewall. Sometimes websites are censored or the user is behind a captive
    portal (like a coffeeshop wifi) which will ruin results.
    """

    def __init__(self):
        SimpleTask.__init__(self, 'CheckIP')
        self._counter = 0

    def process(self, item):
        # Check if we are behind firewall/proxy

        if self._counter <= 0:
            item.log_output('Checking IP address.')
            ip_set = set()

            ip_set.add(socket.gethostbyname('twitter.com'))
            ip_set.add(socket.gethostbyname('facebook.com'))
            ip_set.add(socket.gethostbyname('youtube.com'))
            ip_set.add(socket.gethostbyname('microsoft.com'))
            ip_set.add(socket.gethostbyname('icanhas.cheezburger.com'))
            ip_set.add(socket.gethostbyname('archiveteam.org'))

            if len(ip_set) != 6:
                item.log_output('Got IP addresses: {0}'.format(ip_set))
                item.log_output(
                    'Are you behind a firewall/proxy? That is a big no-no!')
                raise Exception(
                    'Are you behind a firewall/proxy? That is a big no-no!')

        # Check only occasionally
        if self._counter <= 0:
            self._counter = 10
        else:
            self._counter -= 1


class PrepareDirectories(SimpleTask):
    """
    A task that creates temporary directories and initializes filenames.

    It initializes these directories, based on the previously set item_name:
    item["item_dir"] = "%{data_dir}/%{item_name}"
    item["warc_file_base"] = "%{warc_prefix}-%{item_name}-%{timestamp}"

    These attributes are used in the following tasks, e.g., the Wget call.

    * set warc_prefix to the project name.
    * item["data_dir"] is set by the environment: it points to a working
    directory reserved for this item.
    * use item["item_dir"] for temporary files
    """

    def __init__(self, warc_prefix):
        SimpleTask.__init__(self, 'PrepareDirectories')
        self.warc_prefix = warc_prefix

    def process(self, item):
        item_name = item['item_name']
        escaped_item_name = item_name.replace(':', '_').replace('/', '_').replace('~', '_')
        item_hash = hashlib.sha1(item_name.encode('utf-8')).hexdigest()
        dirname = '/'.join((item['data_dir'], item_hash))

        if os.path.isdir(dirname):
            shutil.rmtree(dirname)

        os.makedirs(dirname)

        item['item_dir'] = dirname
        item['warc_file_base'] = '%s-%s-%s' % (self.warc_prefix, item_hash,
                                               time.strftime('%Y%m%d-%H%M%S'))

        open('%(item_dir)s/%(warc_file_base)s.warc.gz' % item, 'w').close()
        open('%(item_dir)s/%(warc_file_base)s_data.txt' % item, 'w').close()


class Deduplicate(SimpleTask):
    """
    This task removes any duplicates found in the current task.
    """

    def __init__(self):
        SimpleTask.__init__(self, 'Deduplicate')

    def process(self, item):
        digests = {}
        input_filename = '%(item_dir)s/%(warc_file_base)s.warc.gz' % item
        output_filename = '%(item_dir)s/%(warc_file_base)s-deduplicated.warc.gz' % item

        with open(input_filename, 'rb') as f_in, \
                open(output_filename, 'wb') as f_out:
            writer = WARCWriter(filebuf=f_out, gzip=True)

            for record in ArchiveIterator(f_in):
                url = record.rec_headers.get_header('WARC-Target-URI')

                if url is not None and url.startswith('<'):
                    url = re.search('^<(.+)>$', url).group(1)
                    record.rec_headers.replace_header('WARC-Target-URI', url)

                if record.rec_headers.get_header('WARC-Type') == 'response':
                    digest = record.rec_headers.get_header('WARC-Payload-Digest')

                    if digest in digests:
                        writer.write_record(
                            self._record_response_to_revisit(writer, record,
                                                             digests[digest])
                        )

                    else:
                        digests[digest] = (
                            record.rec_headers.get_header('WARC-Record-ID'),
                            record.rec_headers.get_header('WARC-Date'),
                            record.rec_headers.get_header('WARC-Target-URI')
                        )
                        writer.write_record(record)

                elif record.rec_headers.get_header('WARC-Type') == 'warcinfo':
                    record.rec_headers.replace_header('WARC-Filename', output_filename)
                    writer.write_record(record)

                else:
                    writer.write_record(record)

    def _record_response_to_revisit(self, writer, record, duplicate):
        warc_headers = record.rec_headers
        warc_headers.replace_header('WARC-Refers-To', duplicate[0])
        warc_headers.replace_header('WARC-Refers-To-Date', duplicate[1])
        warc_headers.replace_header('WARC-Refers-To-Target-URI', duplicate[2])
        warc_headers.replace_header('WARC-Type', 'revisit')
        warc_headers.replace_header('WARC-Truncated', 'length')
        warc_headers.replace_header('WARC-Profile',
                                    'http://netpreserve.org/warc/1.0/' \
                                    'revisit/identical-payload-digest')
        warc_headers.remove_header('WARC-Block-Digest')
        warc_headers.remove_header('Content-Length')

        return writer.create_warc_record(
            record.rec_headers.get_header('WARC-Target-URI'),
            'revisit',
            warc_headers=warc_headers,
            http_headers=record.http_headers
        )


class MoveFiles(SimpleTask):
    """
    After downloading, this task moves the warc file from the
    item["item_dir"] directory to the item["data_dir"], and removes
    the files in the item["item_dir"] directory.
    """

    def __init__(self):
        SimpleTask.__init__(self, 'MoveFiles')

    def process(self, item):
        if os.path.exists('%(item_dir)s/%(warc_file_base)s.warc' % item):
            raise Exception('Please compile wget with zlib support!')

        os.rename('%(item_dir)s/%(warc_file_base)s.warc.gz' % item,
                  '%(data_dir)s/%(warc_file_base)s.warc.gz' % item)
        os.rename('%(item_dir)s/%(warc_file_base)s_data.txt' % item,
                  '%(data_dir)s/%(warc_file_base)s_data.txt' % item)

        shutil.rmtree('%(item_dir)s' % item)


class WgetArgs(object):
    """
    Setup Wget Lua
    """

    def realize(self, item):
        wget_args = [
            WGET_LUA,
            '-U', USER_AGENT,
            '-nv',
            '--no-cookies',
            '--lua-script', 'sketch.lua',
            '-o', ItemInterpolation('%(item_dir)s/wget.log'),
            '--no-check-certificate',
            '--output-document', ItemInterpolation('%(item_dir)s/wget.tmp'),
            '--truncate-output',
            '-e', 'robots=off',
            '--rotate-dns',
            '--recursive', '--level=inf',
            '--no-parent',
            '--page-requisites',
            '--timeout', '30',
            '--tries', 'inf',
            '--domains', 'sketch.sonymobile.com',
            '--span-hosts',
            '--waitretry', '30',
            '--warc-file', ItemInterpolation('%(item_dir)s/%(warc_file_base)s'),
            '--warc-header', 'operator: Archive Team',
            '--warc-header', 'sketch-dld-script-version: ' + VERSION,
            '--warc-header', ItemInterpolation('sketch-item: %(item_name)s')
        ]

        item_name = item['item_name']
        item_type, item_value = item_name.split(':', 1)

        item['item_type'] = item_type
        item['item_value'] = item_value

        http_client = httpclient.HTTPClient()

        if item_type == 'disco':
            r = http_client.fetch('http://103.230.141.2/sketch/' + item_value, method='GET')
            for s in r.body.decode('utf-8', 'ignore').splitlines():
                s = s.strip()
                if len(s) == 0:
                    continue
        else:
            raise Exception('Unknown item')

        http_client.close()

        if 'bind_address' in globals():
            wget_args.extend(['--bind-address', globals()['bind_address']])
            print('')
            print('*** Wget will bind address at {0} ***'.format(
                globals()['bind_address']))
            print('')

        return realize(wget_args, item)


###########################################################################
# Initialize the project.
#
# This will be shown in the warrior management panel. The logo should not
# be too big. The deadline is optional.
project = Project(
    title='Sketch',
    project_html='''
        <img class="project-logo" alt="Project logo" src="https://sketch.sonymobile.com/assets/images/sketch-logo-c6ae2519fc.svg" height="50px" title=""/>
        <h2>sketch.sonymobile.com <span class="links"><a href="https://sketch.sonymobile.com">Website</a> &middot; <a href="http://tracker.archiveteam.org/sketch/">Leaderboard</a></span></h2>
        <p>Saving Sketch</p>
    '''
)


pipeline = Pipeline(

    CheckIP(),

    # request an item from the tracker (using the universal-tracker protocol)
    # the downloader variable will be set by the warrior environment
    #
    # this task will wait for an item and sets item["item_name"] to the item name
    # before finishing
    GetItemFromTracker('http://%s/%s' % (TRACKER_HOST, TRACKER_ID), downloader,
                       VERSION),

    # create the directories and initialize the filenames (see class above)
    # warc_prefix is the first part of the warc filename
    #
    # this task will set item["item_dir"] and item["warc_file_base"]
    PrepareDirectories(warc_prefix='sketch'),

    # execute Wget+Lua
    #
    # the ItemInterpolation() objects are resolved during runtime
    # (when there is an Item with values that can be added to the strings)
    WgetDownload(
        WgetArgs(),
        max_tries=2,
        # check this: which Wget exit codes count as a success?
        accept_on_exit_code=[0, 4, 8],
        env={
            'item_dir': ItemValue('item_dir'),
            'item_value': ItemValue('item_value'),
            'item_type': ItemValue('item_type'),
            'warc_file_base': ItemValue('warc_file_base')
        }
    ),

    #Deduplicate(),

    # this will set the item["stats"] string that is sent to the tracker
    PrepareStatsForTracker(
        defaults={'downloader': downloader, 'version': VERSION},

        # this is used for the size counter on the tracker:
        # the groups should correspond with the groups set configured on the tracker
        file_groups={
            # there can be multiple groups with multiple files
            # file sizes are measured per group
            'data': [
                ItemInterpolation('%(item_dir)s/%(warc_file_base)s.warc.gz')
            ]
        },
        id_function=stats_id_function,
    ),

    # remove the temporary files, move the warc file from
    # item["item_dir"] to item["data_dir"]
    MoveFiles(),

    # there can be multiple items in the pipeline, but this wrapper ensures
    # that there is only one item uploading at a time
    #
    # the NumberConfigValue can be changed in the configuration panel
    LimitConcurrent(NumberConfigValue(min=1, max=20, default='3',
        name='shared:rsync_threads', title='Rsync threads',
        description='The maximum number of concurrent uploads.'),

        # this upload task asks the tracker for an upload target
        # this can be HTTP or rsync and can be changed in the tracker admin panel
        UploadWithTracker(
            'http://%s/%s' % (TRACKER_HOST, TRACKER_ID),
            downloader=downloader,
            version=VERSION,
            # list the files that should be uploaded.
            # this may include directory names.
            # note: HTTP uploads will only upload the first file on this list
            files=[
                ItemInterpolation('%(data_dir)s/%(warc_file_base)s.warc.gz'),
                ItemInterpolation('%(data_dir)s/%(warc_file_base)s_data.txt')
            ],
            # the relative path for the rsync command
            # (this defines if the files are uploaded to a subdirectory on the server)
            rsync_target_source_path=ItemInterpolation('%(data_dir)s/'),
            rsync_extra_args=[
                '--sockopts=SO_SNDBUF=8388608,SO_RCVBUF=8388608',
                '--recursive',
                '--partial',
                '--partial-dir', '.rsync-tmp',
                '--min-size', '1',
                '--no-compress',
                '--compress-level', '0'
            ]
            ),
    ),

    # if the item passed every task, notify the tracker and report the statistics
    SendDoneToTracker(
        tracker_url='http://%s/%s' % (TRACKER_HOST, TRACKER_ID),
        stats=ItemValue('stats')
    )

)
