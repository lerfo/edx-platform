"""
Unit tests for LMS instructor-initiated background tasks helper functions.

Tests that CSV grade report generation works with unicode emails.

"""
import os
import shutil
from datetime import datetime
import urllib

import ddt
from mock import Mock, patch

from django.test.testcases import TestCase
from pytz import UTC

from xmodule.modulestore.tests.factories import CourseFactory

from instructor_task.tasks_helper import (
    upload_grades_csv,
    upload_students_csv,
    push_ora2_responses_to_s3,
    UPDATE_STATUS_FAILED,
    UPDATE_STATUS_SUCCEEDED,
)

TEST_COURSE_ORG = 'edx'
TEST_COURSE_NAME = 'test_course'
TEST_COURSE_NUMBER = '1.23x'
from instructor_task.models import ReportStore
from instructor_task.tests.test_base import InstructorTaskCourseTestCase, TestReportMixin


@ddt.ddt
class TestInstructorGradeReport(TestReportMixin, InstructorTaskCourseTestCase):
    """
    Tests that CSV grade report generation works.
    """
    def setUp(self):
        self.course = CourseFactory.create()

    @ddt.data([u'student@example.com', u'ni\xf1o@example.com'])
    def test_unicode_emails(self, emails):
        """
        Test that students with unicode characters in emails is handled.
        """
        for i, email in enumerate(emails):
            self.create_student('student{0}'.format(i), email)

        self.current_task = Mock()
        self.current_task.update_state = Mock()
        with patch('instructor_task.tasks_helper._get_current_task') as mock_current_task:
            mock_current_task.return_value = self.current_task
            result = upload_grades_csv(None, None, self.course.id, None, 'graded')
        num_students = len(emails)
        self.assertDictContainsSubset({'attempted': num_students, 'succeeded': num_students, 'failed': 0}, result)

    @patch('instructor_task.tasks_helper._get_current_task')
    @patch('instructor_task.tasks_helper.iterate_grades_for')
    def test_grading_failure(self, mock_iterate_grades_for, _mock_current_task):
        """
        Test that any grading errors are properly reported in the
        progress dict and uploaded to the report store.
        """
        # mock an error response from `iterate_grades_for`
        mock_iterate_grades_for.return_value = [
            (self.create_student('username', 'student@example.com'), {}, 'Cannot grade student')
        ]
        result = upload_grades_csv(None, None, self.course.id, None, 'graded')
        self.assertDictContainsSubset({'attempted': 1, 'succeeded': 0, 'failed': 1}, result)

        report_store = ReportStore.from_config()
        self.assertTrue(any('grade_report_err' in item[0] for item in report_store.links_for(self.course.id)))


@ddt.ddt
class TestStudentReport(TestReportMixin, InstructorTaskCourseTestCase):
    """
    Tests that CSV student profile report generation works.
    """
    def setUp(self):
        self.course = CourseFactory.create()

    def test_success(self):
        self.create_student('student', 'student@example.com')
        task_input = {'features': []}
        with patch('instructor_task.tasks_helper._get_current_task'):
            result = upload_students_csv(None, None, self.course.id, task_input, 'calculated')
        report_store = ReportStore.from_config()
        links = report_store.links_for(self.course.id)

        self.assertEquals(len(links), 1)
        self.assertDictContainsSubset({'attempted': 1, 'succeeded': 1, 'failed': 0}, result)

    @ddt.data([u'student', u'student\xec'])
    def test_unicode_usernames(self, students):
        """
        Test that students with unicode characters in their usernames
        are handled.
        """
        for i, student in enumerate(students):
            self.create_student(username=student, email='student{0}@example.com'.format(i))

        self.current_task = Mock()
        self.current_task.update_state = Mock()
        task_input = {
            'features': [
                'id', 'username', 'name', 'email', 'language', 'location',
                'year_of_birth', 'gender', 'level_of_education', 'mailing_address',
                'goals'
            ]
        }
        with patch('instructor_task.tasks_helper._get_current_task') as mock_current_task:
            mock_current_task.return_value = self.current_task
            result = upload_students_csv(None, None, self.course.id, task_input, 'calculated')
        #This assertion simply confirms that the generation completed with no errors
        num_students = len(students)
        self.assertDictContainsSubset({'attempted': num_students, 'succeeded': num_students, 'failed': 0}, result)


class TestInstructorOra2Report(TestCase):
    """
    Tests that ORA2 response report generation works.
    """
    def setUp(self):
        self.course = CourseFactory.create(org=TEST_COURSE_ORG,
                                           number=TEST_COURSE_NUMBER,
                                           display_name=TEST_COURSE_NAME)

        self.current_task = Mock()
        self.current_task.update_state = Mock()

    def tearDown(self):
        if os.path.exists(settings.ORA2_RESPONSES_DOWNLOAD['ROOT_PATH']):
            shutil.rmtree(settings.ORA2_RESPONSES_DOWNLOAD['ROOT_PATH'])

    def test_report_fails_if_error(self):
        with patch('instructor_task.tasks_helper.collect_ora2_data') as mock_collect_data:
            mock_collect_data.side_effect = KeyError

            with patch('instructor_task.tasks_helper._get_current_task') as mock_current_task:
                mock_current_task.return_value = self.current_task

                self.assertEqual(push_ora2_responses_to_s3(None, None, self.course.id, None, 'generated'), UPDATE_STATUS_FAILED)

    @patch('instructor_task.tasks_helper.datetime')
    def test_report_stores_results(self, mock_time):
        start_time = datetime.now(UTC)
        mock_time.now.return_value = start_time

        test_header = ['field1', 'field2']
        test_rows = [['row1_field1', 'row1_field2'], ['row2_field1', 'row2_field2']]

        with patch('instructor_task.tasks_helper._get_current_task') as mock_current_task:
            mock_current_task.return_value = self.current_task

            with patch('instructor_task.tasks_helper.collect_ora2_data') as mock_collect_data:
                mock_collect_data.return_value = (test_header, test_rows)

                with patch('instructor_task.models.LocalFSReportStore.store_rows') as mock_store_rows:
                    return_val = push_ora2_responses_to_s3(None, None, self.course.id, None, 'generated')

                    timestamp_str = start_time.strftime('%Y-%m-%d-%H%M')
                    course_id_string = urllib.quote(self.course.id.to_deprecated_string().replace('/', '_'))
                    filename = u'{}_ORA2_responses_{}.csv'.format(course_id_string, timestamp_str)

                    self.assertEqual(return_val, UPDATE_STATUS_SUCCEEDED)
                    mock_store_rows.assert_called_once_with(self.course.id, filename, [test_header] + test_rows)
