import unittest
from unittest import mock

from ycluster.utils import authentik_manager


class InvitationProjectTests(unittest.TestCase):
    def test_matched_invitations_filters_by_email(self):
        api = mock.Mock()
        api.get.return_value = {'results': [
            {'fixed_data': {'email': 'a'}},
            {'fixed_data': {'email': 'b'}},
        ]}
        got = authentik_manager._matched_invitations(api, 'a')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['fixed_data']['email'], 'a')

    def test_result_messages(self):
        self.assertEqual(
            authentik_manager._invitation_project_result('user', 'proj', 2),
            "tagged 2 invitations for user with project 'proj'")
        self.assertEqual(
            authentik_manager._invitation_project_result('user', '', 1),
            'cleared project tag from 1 invitation for user')

    def test_set_requires_invitation(self):
        with mock.patch.object(authentik_manager, '_matched_invitations',
                               return_value=[]), \
                mock.patch.object(authentik_manager, 'AuthentikAPI',
                                  mock.Mock()):
            with self.assertRaisesRegex(RuntimeError, 'no outstanding'):
                authentik_manager.set_invitation_project('user', 'proj')
