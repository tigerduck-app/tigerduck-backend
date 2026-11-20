# TigerDuck backend

The server behind the TigerDuck apps for NTUST students, and the portal its operators use.

## Language

**Course announcement**:
The first post of a discussion in a Moodle course's news forum. It is not a bulletin, which
is a notice on the school's or the app's notice board.
_Avoid_: Announcement, course news, forum post

**Extraction**:
The facts the LLM returns for one version of a course announcement.
_Avoid_: LLM output, parse result

**Review item**:
One extracted course change for one course code, with an add, replace or remove action.
_Avoid_: Suggestion, proposal, candidate

**Pending review**:
A review item waiting for an operator's decision.
_Avoid_: Queued, unreviewed

**Course change** (課程異動):
A one-off difference from a course's regular meeting on given class days, such as a cancellation,
another room, another time or an assessment.
_Avoid_: Schedule change, adjustment

**Assessment** (評量):
A class meeting in which every student of the course is assessed at the same time: a midterm or
final exam, a quiz, or a presentation the whole class gives that day.
_Avoid_: Exam (for the category), test, 考試/簡報

**Term exam** (段考):
The midterm and final exams together; not a kind of assessment of its own.
_Avoid_: Sectional exam, unit exam

**Presentation** (簡報):
A talk students give in front of the class; an assessment only when the whole class presents that day.
_Avoid_: 報告 (reads as a written report)

**Online class** (線上):
A class meeting held online instead of in its room, live or self-paced.
_Avoid_: Remote class, 非同步, 線上自學 (as a kind)

**Note** (備註):
A course change that fits no other kind, kept as free text; the last resort, since a student sees
its content only by opening the course.
_Avoid_: Remark, memo
