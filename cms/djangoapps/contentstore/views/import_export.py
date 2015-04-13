"""
These views handle all actions in Studio related to import and exporting of
courses
"""
import logging
from opaque_keys import InvalidKeyError
import re

from contentstore.utils import reverse_course_url, reverse_usage_url

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.views.decorators.http import require_http_methods

from django_future.csrf import ensure_csrf_cookie
from edxmako.shortcuts import render_to_response
from opaque_keys.edx.keys import CourseKey

from student.auth import has_course_author_access
from util.views import ensure_valid_course_key
from xmodule.modulestore.django import modulestore

from urllib import urlencode


__all__ = ['import_handler', 'export_handler']


log = logging.getLogger(__name__)


# Regex to capture Content-Range header ranges.
CONTENT_RE = re.compile(
    r"(?P<start>\d{1,11})-(?P<stop>\d{1,11})/(?P<end>\d{1,11})"
)


# pylint: disable=unused-argument
@login_required
@ensure_csrf_cookie
@require_http_methods(("GET",))
@ensure_valid_course_key
def import_handler(request, course_key_string):
    """
    The restful handler for the import page.

    GET
        html: return html page for import page
    """
    course_key = CourseKey.from_string(course_key_string)
    if not has_course_author_access(request.user, course_key):
        raise PermissionDenied()

    course_module = modulestore().get_course(course_key)
    return render_to_response('import.html', {
        'context_course': course_module,
        'successful_import_redirect_url': reverse_course_url(
            'course_handler',
            course_key
        ),
        'import_status_url': reverse(
            "course_import_status_handler",
            kwargs={
                "course_key_string": unicode(course_key),
                "filename": "fillerName"
            }
        ),
        'import_url': reverse(
            "course_import_export_handler",
            kwargs={
                "course_key_string": unicode(course_key),
            }
        )
    })


# pylint: disable=unused-argument
@ensure_csrf_cookie
@login_required
@require_http_methods(("GET",))
@ensure_valid_course_key
def export_handler(request, course_key_string):
    """
    The restful handler for the export page.

    GET
        html: return html page for import page
    """
    error = request.GET.get('error', None)
    error_message = request.GET.get('error_message', None)
    failed_module = request.GET.get('failed_module', None)
    unit = request.GET.get('unit', None)
    parent = request.GET.get('parent', None)

    if parent:
        try:
            parent = CourseKey.from_string(parent)
        except InvalidKeyError:
            parent = None

    course_key = CourseKey.from_string(course_key_string)
    if not has_course_author_access(request.user, course_key):
        raise PermissionDenied()

    course_module = modulestore().get_course(course_key)

    export_url = reverse(
        "course_import_export_handler",
        kwargs={
            "course_key_string": unicode(course_key),
        }
    ) + '?accept=application/x-tgz'

    export_url += "&{0}".format(
        urlencode({
            "redirect": reverse_course_url(
                'export_handler',
                unicode(course_key)
            )
        })
    )

    if error:
        return render_to_response('export.html', {
            "context_course": course_module,
            "export_url": export_url,
            "in_err": error,
            "unit": unit,
            "failed_module": failed_module,
            "edit_unit_url":
                reverse_usage_url("container_handler", parent.location)
                if parent else "",
            "course_home_url": reverse_course_url("course_handler", course_key),
            "raw_err_msg": error_message
        })
    else:
        return render_to_response('export.html', {
            "context_course": course_module,
            "export_url": export_url
        })
