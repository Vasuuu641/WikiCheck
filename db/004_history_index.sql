-- History lookups by article and time: "versions of page X around date Y" and the per-article
-- version count / first-edit date read (page_id, row_start) instead of scanning the table.
ALTER TABLE article_state ADD KEY page_start (page_id, row_start);
