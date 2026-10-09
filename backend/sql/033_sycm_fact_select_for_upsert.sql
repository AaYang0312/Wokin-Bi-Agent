-- 033：生意参谋事实表补 SELECT，让 bi_app 的 upsert 能规划成功。
--
-- 依赖：028（bi.sycm_shop_daily / bi.sycm_customer_daily）、031（INSERT+UPDATE）。
-- 001–032 零改动；GRANT 重放幂等。
--
-- 031 只授了 INSERT、UPDATE。导入器对这两张表使用
-- INSERT ... ON CONFLICT DO UPDATE。PostgreSQL 在规划整条语句时就要求目标表
-- SELECT，冲突分支没有执行也会 permission denied，上传成功路径因此 503
-- （sycm_upload_write_path_unavailable）。拒绝路径只写台账，不受影响。
-- 本迁移只补 SELECT，不授 DELETE，也不放宽 RLS 或质量门禁。

GRANT SELECT ON bi.sycm_shop_daily, bi.sycm_customer_daily TO bi_app;
