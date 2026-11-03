-- PC - Tracker Student Lookups
-- One row per Spreadsheets/SQL roster student (same cohort as card 13022 "PC - Batch Roster").
-- Feeds Project_Clearance_Tracker "Raw - SS"/"Raw -SQL":
--   activity_last_30days    -> column Z  "Activity"   (replaces the static "Activity" tab, last pasted 3 Jun 2026)
--   acad_interview_persona  -> Raw - SS column AB "Persona" (replaces a hand-typed column that had drifted out of row alignment)
--
-- Activity = any of: 1:1 session both sides joined, arena/playlist attempt, assignment attempt,
-- live lecture joined, meeting joined (same five sources as card 11385 "User-Activty-Query - WoW").
-- Checked against the old Activity tab as of 3 Jun 2026: 236/249 sampled students agree on the 30-day flag.
--
-- Save as a native question on the "Newton School" database (no filters), then put its ID in
-- TRACKER_LOOKUPS_CARD_ID in project_clearance_refresh.py.  Runs in ~2-3 minutes.
WITH params AS (SELECT now() AS as_of, now() - INTERVAL '30 days' AS since),
refund AS (
    SELECT DISTINCT ccum.id AS lu_cum_id
    FROM courses_courseusermapping au
    JOIN courses_courseuserlabelmapping l ON l.course_user_mapping_id = au.id AND l.label_id = 677
    JOIN courses_courseusermapping ccum ON ccum.admin_course_user_mapping_id = au.id
),
cohort AS MATERIALIZED (
    SELECT DISTINCT cum.user_id
    FROM courses_courseusermapping cum
    JOIN courses_course cc ON cc.id = cum.course_id
    WHERE cum.status = 8
      AND cc.course_structure_id IN (50, 193, 52)
      AND cc.start_timestamp >= '2025-01-01'
      AND cc.title NOT IN ('Spreadsheets (T)')
      AND cum.id NOT IN (SELECT lu_cum_id FROM refund)
),
act_onetoone AS (
    SELECT s.booked_by_id AS user_id, MAX(s.start_timestamp) AS ts
    FROM video_sessions_onetoone s
    JOIN cohort c ON c.user_id = s.booked_by_id
    CROSS JOIN params p
    WHERE s.one_to_one_status = 2
      AND s.start_timestamp >= p.since AND s.start_timestamp < p.as_of
      AND EXISTS (SELECT 1 FROM video_sessions_onetoonecourseuserreport r
                  JOIN courses_courseusermapping cum ON cum.id = r.course_user_mapping_id
                  WHERE r.one_to_one_id = s.id AND r.report_type = 4 AND r.join_time IS NOT NULL
                    AND cum.user_id = s.booked_by_id)
      AND EXISTS (SELECT 1 FROM video_sessions_onetoonecourseuserreport r
                  JOIN courses_courseusermapping cum ON cum.id = r.course_user_mapping_id
                  WHERE r.one_to_one_id = s.id AND r.report_type = 4 AND r.join_time IS NOT NULL
                    AND cum.user_id = s.booked_with_id)
    GROUP BY 1
),
act_arena AS (
    SELECT m.user_id, MAX(m.started_at) AS ts
    FROM assignments_milestoneuserquestionmapping m
    JOIN cohort c ON c.user_id = m.user_id
    CROSS JOIN params p
    WHERE m.started_at >= p.since AND m.started_at < p.as_of
    GROUP BY 1
),
act_assignment AS (
    SELECT ccum.user_id, MAX(q.started_at) AS ts
    FROM cohort c
    JOIN courses_courseusermapping ccum ON ccum.user_id = c.user_id AND ccum.status = 8
    JOIN courses_course cc ON cc.id = ccum.course_id
     AND cc.course_structure_id IN (11,14,20,26,50,51,52,53,54,55,56,57,58,59,60,72,94,95,131,132,191,193,269,270)
    JOIN assignments_assignmentcourseusermapping acum ON acum.course_user_mapping_id = ccum.id
    JOIN assignments_assignment aa ON aa.id = acum.assignment_id AND aa.course_id = ccum.course_id
     AND aa.original_assignment_type IN (1,2,3,4)
    JOIN assignments_assignmentcourseuserquestionmapping q ON q.assignment_course_user_mapping_id = acum.id
    CROSS JOIN params p
    WHERE q.started_at >= p.since AND q.started_at < p.as_of
    GROUP BY 1
),
act_lecture AS (
    SELECT ccum.user_id, MAX(r.join_time) AS ts
    FROM cohort c
    JOIN courses_courseusermapping ccum ON ccum.user_id = c.user_id AND ccum.status = 8
    JOIN video_sessions_lecturecourseuserreport r ON r.course_user_mapping_id = ccum.id
    CROSS JOIN params p
    WHERE r.report_type = 4 AND r.join_time >= p.since AND r.join_time < p.as_of
    GROUP BY 1
),
act_meeting AS (
    SELECT ccum.user_id, MAX(r.join_time) AS ts
    FROM cohort c
    JOIN courses_courseusermapping ccum ON ccum.user_id = c.user_id
    JOIN video_sessions_meetingcourseuserreport r ON r.course_user_mapping_id = ccum.id
    CROSS JOIN params p
    WHERE r.report_type = 4 AND r.join_time >= p.since AND r.join_time < p.as_of
    GROUP BY 1
),
persona AS (   -- Acad Interview form 4541, question 1037; latest form per student
    SELECT DISTINCT ON (i.user_id) i.user_id, COALESCE(fa.text, a.other_answer) AS persona
    FROM feedback_feedbackformusermapping u
    JOIN feedback_feedbackformuserquestionanswermapping a
      ON a.feedback_form_user_mapping_id = u.id AND a.feedback_question_id = 1037
    LEFT JOIN feedback_feedbackformuserquestionanswerm2m mm ON mm.feedback_form_user_question_answer_mapping_id = a.id
    LEFT JOIN feedback_feedbackanswer fa ON fa.id = mm.feedback_answer_id
    JOIN users_usercourseinvite i ON i.id = u.entity_object_id
    JOIN cohort c ON c.user_id = i.user_id
    WHERE u.feedback_form_id = 4541
      AND COALESCE(fa.text, a.other_answer) IS NOT NULL
    ORDER BY i.user_id, u.created_at DESC, u.id DESC
),
latest AS (
    SELECT c.user_id, GREATEST(o.ts, a.ts, s.ts, l.ts, m.ts) AS latest_activity_at
    FROM cohort c
    LEFT JOIN act_onetoone o ON o.user_id = c.user_id
    LEFT JOIN act_arena a ON a.user_id = c.user_id
    LEFT JOIN act_assignment s ON s.user_id = c.user_id
    LEFT JOIN act_lecture l ON l.user_id = c.user_id
    LEFT JOIN act_meeting m ON m.user_id = c.user_id
)
SELECT
    lt.user_id,
    lt.latest_activity_at,
    CASE WHEN lt.latest_activity_at >= p.as_of - INTERVAL '7 days'  THEN 'Yes' ELSE 'No' END AS activity_last_7days,
    CASE WHEN lt.latest_activity_at >= p.as_of - INTERVAL '15 days' THEN 'Yes' ELSE 'No' END AS activity_last_15days,
    CASE WHEN lt.latest_activity_at IS NOT NULL                    THEN 'Yes' ELSE 'No' END AS activity_last_30days,
    pe.persona AS acad_interview_persona
FROM latest lt
CROSS JOIN params p
LEFT JOIN persona pe ON pe.user_id = lt.user_id
ORDER BY lt.user_id
