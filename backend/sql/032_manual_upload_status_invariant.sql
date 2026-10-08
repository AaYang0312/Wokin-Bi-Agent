-- M6：manual_uploads 的 status ⇔ reason_code 复合不变式（M4 遗留 P2 收口）。
--
-- 依赖：028（bi.manual_uploads）、031（status/reason_code 词表 CHECK）。
-- 001–031 零改动；重放幂等（沿用 031 先例：DROP IF EXISTS + ADD）。
--
-- 契约（031 词表之上的配对不变式）：
--   status='accepted'  ⇒ reason_code IS NULL   （接受行没有拒绝原因）
--   status='rejected'  ⇒ reason_code 非空       （拒绝行必带稳定原因码）
-- 词表本身（status ∈ {accepted,rejected}、reason_code 七词）由 031 的两份
-- CHECK 继续把守，本迁移只加"配对"这一层：两层合起来，台账里不存在
-- 既接受又带原因、或既拒绝又说不出原因的行。应用时表内须无违反行
-- （accepted+原因码或 rejected+NULL 的存量行会使 ADD 失败——那是显式
-- fail，不是静默改写历史）。

ALTER TABLE bi.manual_uploads DROP CONSTRAINT
  IF EXISTS manual_uploads_status_reason_invariant;
ALTER TABLE bi.manual_uploads ADD CONSTRAINT
  manual_uploads_status_reason_invariant CHECK (
    (status = 'accepted' AND reason_code IS NULL) OR
    (status = 'rejected' AND reason_code IS NOT NULL));

COMMENT ON CONSTRAINT manual_uploads_status_reason_invariant ON bi.manual_uploads IS
  'M6 配对不变式：accepted ⇒ reason_code 为空；rejected ⇒ reason_code 非空（词表见 031）。';
