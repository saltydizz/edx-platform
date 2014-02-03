"""
Staff grading of uploaded assignments.

Assignment files are uploaded to an s3 bucket.  Metadata is stored in a SimpleDB.
Staff can see multiple students' assignments, enter grades, comments, and upload annotated files (e.g. PDF).
Students receive grades and comments when visiting this module.
"""

import json
import logging

from lxml import etree
from pkg_resources import resource_string
import datetime
import time
import urllib

from django.http import Http404
from django.conf import settings
from webob import Response

from pytz import UTC
from boto import connect_sdb
from boto.s3.connection import S3Connection
from boto.s3.key import Key

from open_ended_grading_classes.openendedchild import upload_to_s3
from .progress import Progress

from xmodule.x_module import XModule
from xmodule.editing_module import TabsEditingDescriptor
from xmodule.raw_module import EmptyDataRawDescriptor
from xmodule.xml_module import is_pointer_tag, name_to_pathname, deserialize_field
from xmodule.modulestore import Location
from xblock.fields import Scope, String, Boolean, List, Integer, ScopeIds, Float
from .fields import Timedelta, Date
from xmodule.fields import RelativeTime
from xmodule.seq_module import SequenceDescriptor
from xmodule.raw_module import RawDescriptor

from xmodule.modulestore.inheritance import InheritanceKeyValueStore
from xblock.runtime import DbModel
log = logging.getLogger(__name__)

#-----------------------------------------------------------------------------

def local_upload_to_s3(file_to_upload, keyname, s3_interface):
    '''
    Upload file to S3 using provided keyname.

    Returns:
        public_url: URL to access uploaded file
    '''

    log.info("upload_to_s3: s3_inteface=%s" % s3_interface)
    log.info("keyname=%s" % keyname)

    conn = S3Connection(s3_interface['access_key'], s3_interface['secret_access_key'])
    bucketname = str(s3_interface['storage_bucket_name'])
    bucket = conn.lookup(bucketname.lower())
    if not bucket:
        bucket = conn.create_bucket(bucketname.lower())

    log.info("upload_to_s3: bucket=%s" % bucket)

    k = Key(bucket)
    k.key = keyname
    k.set_metadata('filename', file_to_upload.name)
    k.set_contents_from_file(file_to_upload)

    k.set_acl("public-read")
    public_url = k.generate_url(60 * 60 * 24 * 365)   # URL timeout in seconds.

    return public_url


#-----------------------------------------------------------------------------


class StaffGradingFields(object):
    """Fields for `StaffGradingModule` and `StaffGradingDescriptor`."""
    display_name = String(
        display_name="Display Name", help="Display name for this module.",
        default="Upload Assignment",
        scope=Scope.settings
    )
    due = Date(help="Date that this problem is due by; uploads allowed until due date is past", scope=Scope.settings)
    done = Boolean(help="Whether the student has answered the problem", scope=Scope.user_state)
    score = Float(
        display_name="Grade score",
        help=("Grade score given to assignment by staff."),
        values={"min": 0, "step": .1},
        scope=Scope.user_state
    )
    the_max_score = Float(
        display_name="Maximum score",
        help=("Maximum grade score given to assignment by staff."),
        values={"min": 0, "step": .1},
        default=100,
        scope=Scope.settings
    )
    graceperiod = Timedelta(
        help="Amount of time after the due date that submissions will be accepted",
        scope=Scope.settings
    )


class StaffGradingModule(StaffGradingFields, XModule):
    """
    XML source example:

        <staffgrading display_name="Upload assignment">
        </video>
    """
    icon_class = 'problem'

    def __init__(self, *args, **kwargs):
        super(StaffGradingModule, self).__init__(*args, **kwargs)

        # self.storage = settings.DEFAULT_FILE_STORAGE	# s3 bucket
        self.s3_interface = self.system.s3_interface	# dict with access_key, secret_access_key, storage_bucket_name
        self.user = self.system.get_real_user(self.system.anonymous_student_id)

        due_date = self.due

        if self.graceperiod is not None and due_date:
            self.close_date = due_date + self.graceperiod
        else:
            self.close_date = due_date

        self.fstat = self.FileStatus(self)	# file status from SimpleDB
        self.update_score()

    def update_score(self):
        '''
        Update the "score" variable from the grade stored in the SimpleDB FileStatus table.
        '''
        rset = self.fstat.get_status()
        if rset and rset[0].get('score', None) is not None:
            self.score = rset[0].get('score')
            log.info("Set score for uploaded assignment by %s to %s" % (self.user, self.score))
        elif rset:
            self.score = None
            log.info("Set score for uploaded assignment by %s to %s" % (self.user, self.score))
            
    def is_graded(self):
        '''
        Return True if this assignment has been graded
        '''
        return self.score is not None

    def get_score(self):
        """
        Access the problem's score
        """
        return self.score

    def max_score(self):
        """
        Access the problem's max score
        """
        return self.the_max_score

    def handle_ajax(self, dispatch, data):
        log.info('staff_grading ajax dispatch=%s, data=%s' % (dispatch, data))

        if dispatch=='upload':
            link = self.upload_file(data)
            return json.dumps({'msg':'ok', 'url': link})

        elif dispatch=='download':
            log.info("download, data=%s" % data)
            log.info('uname=%s' % data.get('uname',''))
            log.info('fn=%s' % data.get('fn',''))
            return self.do_download(data)

        elif dispatch=='list':
            log.info("list")
            return self.list_submissions(data)

        elif dispatch=='score' and self.system.user_is_staff:
            log.info("score")
            return self.enter_score(data)

        elif dispatch=='unscore' and self.system.user_is_staff:
            log.info("unscore")
            return self.remove_score(data)

        else:
            log.debug(u"GET {0}".format(data))
            log.debug(u"DISPATCH {0}".format(dispatch))
        raise Http404()

    def is_past_due(self):
        """
        Is it now past this problem's due date, including grace period?
        """
        return (self.close_date is not None and
                datetime.datetime.now(UTC()) > self.close_date)

    def get_html(self):
        rs = self.fstat.get_status()
        
        context = {'s3_interface': self.s3_interface,
                   'is_staff': self.system.user_is_staff,
                   'ajax_url': self.system.ajax_url,
                   'user': self.user,
                   'max_score': self.max_score(),
                   'rs': rs,
                   'display_name': self.display_name,
                   'is_past_due': self.is_past_due(),
                   'is_graded': self.is_graded(),
        }
        return self.system.render_template('staff_grading.html', context)
            
    class FileStatus(object):
        '''
        File metadata, stored in SimpleDB
        
        Each item in the staff_grading domain has these attributes:
            
            - course_id
            - module_id
            - anon_user_id (system.anonymous_student_id)
            - username (via get_real_user)
            - name (student name)
            - s3_filename (in bucket on s3)
            - student_filename (original filename given by student)
            - created (date time stamp of last upload)
            - graded_dt (date time stamp of when graded)
            - approved_dt (date time stamp of when approved)
            - graded_by (username of staff who graded)
            - approved_by (username of staff who approved grade)
            - staff_comments (comments from staff)
            - annotated_filename (in bucket on s3)
            - score (grade from staff, float)
            
        (course_id, module_id, username) should be unique (one file per staff grading download).
        '''
        def __init__(self, module):
            self.sdb = connect_sdb(module.s3_interface['access_key'], module.s3_interface['secret_access_key'])
            self.domain = self.sdb.create_domain('staff_grading')
            self.module = module
            
        def create_status(self, s3_filename, student_filename):
            '''
            Create new status record, done when uploading file for the first time
            '''
            module = self.module
            status = {'course_id': module.course_id,
                      'module_id': str(module.location),
                      'username': module.user.username,
                      'name': module.user.profile.name,
                      'anon_user_id': module.system.anonymous_student_id,
                      's3_filename': s3_filename,
                      'student_filename': student_filename,
                      'created': datetime.datetime.now(),
                      }
            iname = ':'.join([module.course_id, str(module.location), module.user.username])
            item = self.domain.new_item(iname)
            for k, v in status.items():
                item[k] = v
            item.save()
            log.info("Saved SDB item %s" % item)

        def update_status(self, score, comments, extra_match=None):
            '''
            Update grade and comments.
            '''
            rset = self.get_all_status(extra_match=extra_match)
            if not rset:
                return False, 'No record found'
            item = rset[0]
            if score is None:
                log.info('Removing grade, previously %s' % item)
                item['score'] = None
                if 'staff_comments' in item:
                    item.pop('staff_comments')
                if 'graded_dt' in item:
                    item.pop('graded_dt')
                item['staff_comments'] = None
                item['graded_dt'] = None
            else:
                try:
                    item['score'] = float(score)
                    assert float(score) <= self.module.max_score()
                except Exception as err:
                    log.exception("oops failed to convert score=%s" % score)
                    return False, 'Bad grade value, must be numeric'
                item['staff_comments'] = comments
                item['graded_by'] = self.module.user
                item['graded_dt'] = datetime.datetime.now()
            item.save()
            return True, 'Saved grade and comment for %s (%s)' % (self.module.user, self.module.user.profile.name)

        def get_status(self):
            '''
            Find current status of assignment submission, from SDB
            Return item for current user.
            '''
            return self.get_all_status([ "username = '%s'" % str(self.module.user.username) ])

        def get_all_status(self, extra_match=None):
            module = self.module
            matches = ' and '.join(["course_id = '%s'" % module.course_id,
                                    "module_id = '%s'" % str(module.location),
                                    ] + (extra_match or []))

            select = "select * from staff_grading where %s" % matches
            # log.info('select=%s' % select)
            rs = self.domain.select(select)
            rset = [k for k in rs]
            #for k in rset:
            #    log.info("Found SDB items %s" % k)
            return rset

    def list_submissions(self, data):
        '''
        List all submissions available to be graded.  Paginate.
        '''
        rset = self.fstat.get_all_status()
        return(json.dumps(rset))

    def enter_score(self, data):
        '''
        Enter score and comment for specified users assignment
        '''
        score = data.get('grade','')
        comments = data.get('comments','')
        extra_match = []
        if 'uname' in data and self.system.user_is_staff:
            extra_match.append("username = '%s'" % data['uname'])
        (ok, msg) = self.fstat.update_status(score, comments, extra_match=extra_match)
        return json.dumps({'ok':ok, 'msg': msg})

    def remove_score(self, data):
        '''
        Remove score and comment for specified users assignment
        '''
        extra_match = []
        if 'uname' in data and self.system.user_is_staff:
            extra_match.append("username = '%s'" % data['uname'])
        (ok, msg) = self.fstat.update_status(None, None, extra_match=extra_match)
        return json.dumps({'ok':ok, 'msg': msg})

    def do_download(self, data):
        '''
        Download file from s3. 
        '''
        extra_match = []
        if 'fn' in data:
            extra_match.append("student_filename = '%s'" % data['fn'])
        if 'uname' in data and self.system.user_is_staff:
            extra_match.append("username = '%s'" % data['uname'])
            
        rs = self.fstat.get_all_status(extra_match=extra_match)
        if not rs:
            log.error("No files to download")
            return json.dumps({'ok': False, 'msg':'No files to download'})
        fs = rs[0]
        conn = S3Connection(self.s3_interface['access_key'], self.s3_interface['secret_access_key'])
        bucketname = str(self.s3_interface['storage_bucket_name'])
        bucket = conn.lookup(bucketname.lower())
        if not bucket:
            log.error('cannot get bucket')
            return json.dumps({'ok': False, 'msg':'No files to download'})
        key = bucket.get_key(fs['s3_filename'])
        if not key:
            log.error('missing file %s' % fs['s3_filename'])
            return json.dumps({'ok': False, 'msg':'No files to download'})
        url = key.generate_url(expires_in=20, force_http=True)
        # redir = '<html><head><meta http-equiv="Refresh" content="0; url=%s" /></head>' % url
        log.info('download url=%s' % url)
        return json.dumps({'ok': True, 'url': url})
        

    def make_file_name(self, fname):
        '''
        Construct unique filename for student submission.
        Make this via username + course_id + location + students-file-name
        '''
        safe_fname = unicode(urllib.quote(fname))
        return '%s__%s__%s__%s' % (self.user.username, self.course_id, self.location, safe_fname)
        
    def upload_file(self, data):
        """
        upload file to S3, and update SDB record.
        """
        #files = data.FILES or {}

        fd = data['file'][0]
        log.info("fd = %s" % fd)
        log.info("size=%s" % fd.size)
        log.info("name=%s" % fd.name)

        fd.seek(0)
        file_key = self.make_file_name(fd.name)
        s3_public_url = upload_to_s3(fd, file_key, self.s3_interface)

        log.info("url: %s" % s3_public_url)

        link = '<a href="{0}" target="_blank">{1}</a>'.format(s3_public_url, fd.name)

        self.fstat.create_status(file_key, fd.name)

        return link

    def get_score(self):
        """
        Access the assignment's score
        """
        return self.score


class StaffGradingDescriptor(StaffGradingFields, RawDescriptor):
    # the editing interface can be the same as for sequences -- just a container
    module_class = StaffGradingModule

    has_score = True
    filename_extension = "xml"

    # Specify that this module needs an S3 interface
    needs_s3_interface = True

    def definition_to_xml(self, resource_fs):

        xml_object = etree.Element('staffgrading')
        for child in self.get_children():
            xml_object.append(
                etree.fromstring(child.export_to_xml(resource_fs)))
        return xml_object

