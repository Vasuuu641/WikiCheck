-- THE 30-MINUTE SPIKE. Run with:  make spike
-- Question 1: can a VECTOR INDEX live on a system-versioned table?
-- Question 2: can we load past versions with real edit times (insert_history)?
-- Write down what works and what errors in docs/SPIKE_RESULTS.md and tell the team.

DROP TABLE IF EXISTS spike_vec;
CREATE TABLE spike_vec (
  id INT PRIMARY KEY,
  txt VARCHAR(100),
  emb VECTOR(3) NOT NULL,
  VECTOR INDEX (emb)
) WITH SYSTEM VERSIONING;          -- if this errors: vectors+versioning do not combine. Use the fallback below.

INSERT INTO spike_vec VALUES (1, 'a', VEC_FromText('[0.1,0.2,0.3]')), (2, 'b', VEC_FromText('[0.9,0.1,0.1]'));
SELECT SLEEP(1);
UPDATE spike_vec SET txt = 'a-updated' WHERE id = 1;

SELECT id, txt, VEC_DISTANCE_COSINE(emb, VEC_FromText('[0.1,0.2,0.3]')) AS d
FROM spike_vec FOR SYSTEM_TIME ALL ORDER BY d LIMIT 5;

-- Question 2: real history timestamps
SET @@system_versioning_insert_history = 1;
DROP TABLE IF EXISTS spike_hist;
CREATE TABLE spike_hist (
  id INT,
  v VARCHAR(20),
  row_start TIMESTAMP(6) GENERATED ALWAYS AS ROW START,
  row_end   TIMESTAMP(6) GENERATED ALWAYS AS ROW END,
  PERIOD FOR SYSTEM_TIME (row_start, row_end)
) WITH SYSTEM VERSIONING;

INSERT INTO spike_hist (id, v, row_start, row_end) VALUES
  (1, 'rev-2003', '2003-05-01 00:00:00', '2010-01-01 00:00:00'),
  (1, 'rev-2010', '2010-01-01 00:00:00', '2038-01-19 03:14:07.999999');

SELECT * FROM spike_hist FOR SYSTEM_TIME AS OF '2005-01-01 00:00:00';   -- expect rev-2003
SELECT * FROM spike_hist FOR SYSTEM_TIME AS OF '2020-01-01 00:00:00';   -- expect rev-2010

-- FALLBACK if question 1 fails: keep vectors in a normal table keyed by (page_id, rev_id),
-- then join to the versioned table AFTER the nearest-neighbour subquery (see api/search.py).
