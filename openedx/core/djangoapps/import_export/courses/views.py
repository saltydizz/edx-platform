"""
These views handle all actions in Studio related to import and exporting of
courses
"""
import base64
import logging
from opaque_keys import InvalidKeyError
import os
import re
import shutil
import tarfile
from path import path
from tempfile import mkdtemp

from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.core.files.temp import NamedTemporaryFile
from django.core.servers.basehttp import FileWrapper
from django.http import HttpResponse, Http404
from django.utils.translation import ugettext as _
from django.shortcuts import redirect

from rest_framework import renderers
from rest_framework.authentication import OAuth2Authentication, \
    SessionAuthentication
from rest_framework.decorators import renderer_classes \
    as renderer_classes_decorator
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from xmodule.contentstore.django import contentstore
from xmodule.exceptions import SerializationError
from xmodule.modulestore.django import modulestore
from opaque_keys.edx.keys import CourseKey
from opaque_keys.edx.locator import LibraryLocator
from xmodule.modulestore.xml_importer import import_course_from_xml, import_library_from_xml
from xmodule.modulestore.xml_exporter import export_course_to_xml, export_library_to_xml
from xmodule.modulestore import COURSE_ROOT, LIBRARY_ROOT

from student.auth import has_course_author_access

from openedx.core.lib.extract_tar import safetar_extractall
from util.json_request import JsonResponse
from util.views import ensure_valid_course_key

from urllib import urlencode

log = logging.getLogger(__name__)


# Regex to capture Content-Range header ranges.
CONTENT_RE = re.compile(
    r"(?P<start>\d{1,11})-(?P<stop>\d{1,11})/(?P<end>\d{1,11})"
)


class IsCourseStaff(BasePermission):
    """
    Permission that checks to see if the request user has permission to access
    all course content of the requested course
    """
    def has_permission(self, request, view):
        course_key_string = view.kwargs['course_key_string']
        try:
            course_key = CourseKey.from_string(course_key_string)
        except InvalidKeyError:
            raise Http404

        return has_course_author_access(request.user, course_key)


class ArchiveRenderer(renderers.BaseRenderer):
    """
    A Renderer for compressed tars. It gets used at the content negotiation
    stage, but "render" never actually gets used.
    """
    media_type = "application/x-tgz"
    format = None
    render_style = "binary"

    # pylint: disable=unused-argument
    def render(self, data, media_type=None, render_context=None):
        return data


class FullCourseImportStatus(APIView):
    """
    View the import status of a full course import.
    """
    authentication_classes = (OAuth2Authentication, SessionAuthentication)
    permission_classes = (IsAuthenticated, IsCourseStaff)

    # pylint: disable=unused-argument
    @ensure_valid_course_key
    def get(self, request, course_key_string, filename=None):
        """
        Returns an integer corresponding to the status of a file import.
        These are:

            -X : Import unsuccessful due to some error with X as stage [0-3]
            0 : No status info found (import done or upload still in progress)
            1 : Extracting file
            2 : Validating.
            3 : Importing to mongo
            4 : Import successful

        """
        try:
            session_status = request.session["import_status"]
            status = session_status[course_key_string + filename]
        except KeyError:
            status = 0

        return Response({"ImportStatus": status})


class FullCourseImportExport(APIView):
    """
    Import or export a full course archive.
    """
    authentication_classes = (OAuth2Authentication, SessionAuthentication)
    permission_classes = (IsAuthenticated, IsCourseStaff)
    renderer_classes = (ArchiveRenderer, JSONRenderer)

    def _save_request_status(self, request, key, status):
        """
        Save import status for a course in request session
        """
        session_status = request.session.get("import_status")
        if session_status is None:
            session_status = request.session.setdefault("import_status", {})

        session_status[key] = status
        request.session.save()

    def _export_error_response(self, params, redirect_url=None):
        if redirect_url:
            return redirect("{0}?{1}".format(
                redirect_url,
                urlencode(params)
            ))
        else:
            return JsonResponse(params)

    # pylint: disable=unused-argument
    @ensure_valid_course_key
    @renderer_classes_decorator((ArchiveRenderer,))
    def get(self, request, course_key_string):
        """
        The restful handler for exporting a full course or content library.

        GET
            application/x-tgz: return tar.gz file containing exported course
            json: not supported

        Note that there are 2 ways to request the tar.gz file. The request
        header can specify application/x-tgz via HTTP_ACCEPT, or a query
        parameter can be used (?accept=application/x-tgz).

        If the tar.gz file has been requested but the export operation fails,
        a JSON string will be returned which describes the error
        """
        redirect_url = request.QUERY_PARAMS.get('redirect', None)

        courselike_key = CourseKey.from_string(course_key_string)
        library = isinstance(courselike_key, LibraryLocator)

        if library:
            courselike_module = modulestore().get_library(courselike_key)
        else:
            courselike_module = modulestore().get_course(courselike_key)

        name = courselike_module.url_name
        export_file = NamedTemporaryFile(prefix=name + '.', suffix=".tar.gz")
        root_dir = path(mkdtemp())

        try:
            if library:
                export_library_to_xml(
                    modulestore(),
                    contentstore(),
                    courselike_key,
                    root_dir,
                    name
                )
            else:
                export_course_to_xml(
                    modulestore(),
                    contentstore(),
                    courselike_module.id,
                    root_dir,
                    name
                )

            logging.debug(
                u'tar file being generated at {0}'.format(export_file.name)
            )
            with tarfile.open(name=export_file.name, mode='w:gz') as tar_file:
                tar_file.add(root_dir / name, arcname=name)
        except SerializationError as exc:
            log.exception(
                u'There was an error exporting course %s',
                courselike_key
            )
            unit = None
            failed_item = None
            parent = None
            try:
                failed_item = modulestore().get_item(exc.location)
                parent_loc = modulestore().get_parent_location(
                    failed_item.location
                )

                if parent_loc is not None:
                    parent = modulestore().get_item(parent_loc)
                    if parent.location.category == 'vertical':
                        unit = parent
            except:  # pylint: disable=bare-except
                # if we have a nested exception, then we'll show the more
                # generic error message
                pass

            return self._export_error_response(
                {
                    "context_course": str(courselike_module.location),
                    "error": True,
                    "error_message": str(exc),
                    "failed_module":
                    str(failed_item.location) if failed_item else None,
                    "unit":
                    str(unit.location) if unit else None
                },
                redirect_url=redirect_url
            )
        except Exception as exc:
            log.exception(
                'There was an error exporting course %s',
                courselike_key
            )
            return self._export_error_response(
                {
                    "context_course": courselike_module.url_name,
                    "error": True,
                    "error_message": str(exc),
                    "unit": None
                },
                redirect_url=redirect_url
            )
        finally:
            shutil.rmtree(root_dir / name)

        # The course is all set; return the tar.gz
        wrapper = FileWrapper(export_file)

        response = HttpResponse(wrapper, content_type='application/x-tgz')
        response['Content-Disposition'] = 'attachment; filename={}'.format(
            os.path.basename(
                export_file.name.encode('utf-8')
            )
        )
        response['Content-Length'] = os.path.getsize(export_file.name)
        return response

    # pylint: disable=unused-argument
    @ensure_valid_course_key
    @renderer_classes_decorator((JSONRenderer,))
    def post(self, request, course_key_string):
        """
        The restful handler for importing a course.

        GET
            json: return json import status
        POST or PUT
            json: import a course via the .tar.gz file specified inrequest.FILES
        """
        courselike_key = CourseKey.from_string(course_key_string)

        filename = request.FILES['course-data'].name
        key = unicode(courselike_key) + filename
        data_root = path(settings.GITHUB_REPO_ROOT)
        course_subdir = "{0}-{1}-{2}".format(
            courselike_key.org,
            courselike_key.course,
            courselike_key.run
        )
        course_dir = data_root / course_subdir

        # Do everything in a try-except block to make sure everything is
        # properly cleaned up.
        try:
            # Use sessions to keep info about import progress
            session_status = request.session.setdefault("import_status", {})
            self._save_request_status(request, key, 0)
            if not filename.endswith('.tar.gz'):
                self._save_request_status(request, key, -1)
                return JsonResponse(
                    {
                        'error_message': _(
                            'We only support uploading a .tar.gz file.'
                        ),
                        'stage': -1
                    },
                    status=415
                )

            temp_filepath = course_dir / filename
            if not course_dir.isdir():
                os.mkdir(course_dir)

            logging.debug('importing course to {0}'.format(temp_filepath))

            # Get upload chunks byte ranges
            try:
                matches = CONTENT_RE.search(request.META["HTTP_CONTENT_RANGE"])
                content_range = matches.groupdict()
            except KeyError:    # Single chunk
                # no Content-Range header, so make one that will work
                content_range = {'start': 0, 'stop': 1, 'end': 2}

            # stream out the uploaded files in chunks to disk
            if int(content_range['start']) == 0:
                mode = "wb+"
            else:
                mode = "ab+"
                size = os.path.getsize(temp_filepath)
                # Check to make sure we haven't missed a chunk
                # This shouldn't happen, even if different instances are
                # handling the same session, but it's always better to catch
                # errors earlier.
                if size < int(content_range['start']):
                    self._save_request_status(request, key, -1)
                    log.warning(
                        "Reported range %s does not match size downloaded so "
                        "far %s",
                        content_range['start'],
                        size
                    )
                    return JsonResponse(
                        {
                            'error_message': _(
                                'File upload corrupted. Please try again'
                            ),
                            'stage': -1
                        },
                        status=409
                    )
                # The last request sometimes comes twice. This happens because
                # nginx sends a 499 error code when the response takes too long.
                elif size > int(content_range['stop']) \
                        and size == int(content_range['end']):
                    return JsonResponse({'ImportStatus': 1})

            with open(temp_filepath, mode) as temp_file:
                for chunk in request.FILES['course-data'].chunks():
                    temp_file.write(chunk)

            size = os.path.getsize(temp_filepath)

            if int(content_range['stop']) != int(content_range['end']) - 1:
                # More chunks coming
                return JsonResponse({
                    "files": [{
                        "name": filename,
                        "size": size,
                        "delete_url": "",
                        "delete_type": "",
                        "thumbnail_url": ""
                    }]
                })
        # Send errors to client with stage at which error occurred.
        except Exception as exception:   # pylint: disable=W0703
            self._save_request_status(request, key, -1)
            if course_dir.isdir():
                shutil.rmtree(course_dir)
                log.info(
                    "Course import {0}: Temp data cleared".format(courselike_key)
                )

            log.exception(
                "error importing course"
            )
            return JsonResponse(
                {
                    'error_message': str(exception),
                    'stage': -1
                },
                status=400
            )

        # try-finally block for proper clean up after receiving last chunk.
        try:
            # This was the last chunk.
            log.info("Course import {0}: Upload complete".format(courselike_key))
            self._save_request_status(request, key, 1)

            tar_file = tarfile.open(temp_filepath)
            try:
                safetar_extractall(
                    tar_file,
                    (course_dir + '/').encode('utf-8'))
            except SuspiciousOperation as exc:
                self._save_request_status(request, key, -1)
                return JsonResponse(
                    {
                        'error_message': 'Unsafe tar file. Aborting import.',
                        'suspicious_operation_message': exc.args[0],
                        'stage': -1
                    },
                    status=400
                )
            finally:
                tar_file.close()

            log.info(
                "Course import {0}: Uploaded file extracted".format(courselike_key)
            )
            self._save_request_status(request, key, 2)

            # find the 'course.xml' file
            def get_all_files(directory):
                """
                For each file in the directory, yield a 2-tuple of (file-name,
                directory-path)
                """
                for dirpath, _dirnames, filenames in os.walk(directory):
                    for filename in filenames:
                        yield (filename, dirpath)

            def get_dir_for_fname(directory, filename):
                """
                Returns the dirpath for the first file found in the directory
                with the given name.  If there is no file in the directory with
                the specified name, return None.
                """
                for fname, dirpath in get_all_files(directory):
                    if fname == filename:
                        return dirpath
                return None

            fname = "course.xml"
            dirpath = get_dir_for_fname(course_dir, fname)
            if not dirpath:
                self._save_request_status(request, key, -2)
                return JsonResponse(
                    {

                        'error_message': _(
                            'Could not find the course.xml file in the package.'
                        ),
                        'stage': -2
                    },
                    status=415
                )

            dirpath = os.path.relpath(dirpath, data_root)
            logging.debug('found course.xml at {0}'.format(dirpath))

            log.info(
                "Course import {0}: Extracted file verified".format(courselike_key)
            )
            self._save_request_status(request, key, 3)

            course_items = import_course_from_xml(
                modulestore(),
                request.user.id,
                settings.GITHUB_REPO_ROOT,
                [dirpath],
                load_error_modules=False,
                static_content_store=contentstore(),
                target_id=courselike_key,
            )

            new_location = course_items[0].location
            logging.debug('new course at {0}'.format(new_location))

            log.info(
                "Course import {0}: Course import successful".format(courselike_key)
            )
            self._save_request_status(request, key, 4)

        # Send errors to client with stage at which error occurred.
        except Exception as exception:   # pylint: disable=W0703
            log.exception(
                "error importing course"
            )
            return JsonResponse(
                {
                    'error_message': str(exception),
                    'stage': -session_status[key]
                },
                status=400
            )

        finally:
            if course_dir.isdir():
                shutil.rmtree(course_dir)
                log.info(
                    "Course import {0}: Temp data cleared".format(courselike_key)
                )
            # set failed stage number with negative sign in case of an
            # unsuccessful import
            if session_status[key] != 4:
                self._save_request_status(
                    request,
                    key,
                    -abs(session_status[key])
                )

        return JsonResponse({'status': 'OK'})
