from courseware.access import get_user_role


def check_impersonate(request):
    course_id = request.GET.get('course_id')
    if not course_id:
        return False
    role = get_user_role(request.user, course_id)
    return role == 'instructor'
