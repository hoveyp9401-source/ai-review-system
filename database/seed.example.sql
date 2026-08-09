INSERT INTO teams (code, name, department_name)
VALUES
    ('team-01', 'Team 01', 'AI Department'),
    ('team-02', 'Team 02', 'AI Department'),
    ('team-03', 'Team 03', 'AI Department'),
    ('team-04', 'Team 04', 'AI Department'),
    ('team-05', 'Team 05', 'AI Department'),
    ('team-06', 'Team 06', 'AI Department'),
    ('team-07', 'Team 07', 'AI Department')
ON CONFLICT (code) DO NOTHING;

-- Replace DingTalk IDs and names with the real employee roster.
INSERT INTO users (dingtalk_user_id, employee_no, name, team_id)
SELECT 'dingtalk-user-001', 'E001', 'Example User 001', id
FROM teams
WHERE code = 'team-01'
ON CONFLICT (dingtalk_user_id) DO NOTHING;

INSERT INTO users (dingtalk_user_id, employee_no, name, team_id)
SELECT 'dingtalk-user-002', 'E002', 'Example User 002', id
FROM teams
WHERE code = 'team-01'
ON CONFLICT (dingtalk_user_id) DO NOTHING;
