-- ============================================================
-- Memoria del Hielo — 数据库初始化脚本
-- ============================================================
-- 执行前先创建数据库和用户：
--   CREATE DATABASE memoria_del_hielo
--     CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;
--   CREATE USER 'mhi_user'@'%' IDENTIFIED BY '<password>';
--   GRANT ALL PRIVILEGES ON memoria_del_hielo.* TO 'mhi_user'@'%';
--   FLUSH PRIVILEGES;
-- 然后：USE memoria_del_hielo; SOURCE scripts/init_db.sql;
--
-- ⚠️ 注意：
--   · MariaDB 不支持 WITH PARSER ngram（那是 MySQL 特性）——
--     中文分词在 Python 侧由 jieba 完成。
--   · content_tokenized 列存 jieba 分词结果（空格分隔），
--     供 CJK FULLTEXT 搜索使用。
--   · 非 CJK 文本走 content 列的原有 FULLTEXT。
--   · INSERT 显式列出列名，避免 DEFAULT 列数不匹配。
--
-- ★ 本文件是**合并后的现行 schema**（2026-09-21 与现网库逐列比对过）：
--   历史上依次跑过的 migrate_001~004 与嵌入缓存迁移，效果都已并入本文件。
--   全新安装只跑这一个脚本即可，不必再找那些迁移脚本
--   （它们记录的是"当时怎么改的"，属作者个人项目历史）。

-- ── 主表 ──────────────────────────────────────────────

-- 图书 + 游戏剧情（书级 = 书目卡片：书名+目录向量 + 原文档案；
-- 全文/向量检索主战场在 documents_book_chunk）
CREATE TABLE IF NOT EXISTS documents_book (
    guid              VARCHAR(36) PRIMARY KEY,
    source_path_hash  CHAR(32) NOT NULL DEFAULT '',   -- md5(绝对路径)，幂等导入锚点
    status            VARCHAR(16) NOT NULL DEFAULT 'processing',
        -- 二段事务状态机：processing（一段未完）/ done（两段都成）
    expected_chunks   INT UNSIGNED NOT NULL DEFAULT 0,
        -- 一段结束时的预期 chunk 数；status='done' 且实际数相符才跳过重导
    source_path       VARCHAR(1024) DEFAULT '',       -- 原始文件路径（展示用）
    bucket            VARCHAR(64) NOT NULL,
    tags              TEXT DEFAULT '',
    key_metadata      MEDIUMTEXT DEFAULT '',        -- 书名（原始文件名）
    content           LONGTEXT NOT NULL,             -- 全书文本（清洗后）：GB18030→UTF-8 膨胀 1.5x，超长 txt 可达 19MB+，MEDIUMTEXT(16MB) 会 Data too long
    embedding    VECTOR(1024) NOT NULL,              -- 书目向量（书名+目录，≤2000字符）
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_source (source_path_hash),
    VECTOR INDEX idx_vec_book (embedding) M=16 DISTANCE=cosine
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;

-- 图书 chunk（书桶检索主战场；≤2500 字符保证 BGE-M3 8192 token 物理上限内）
CREATE TABLE IF NOT EXISTS documents_book_chunk (
    chunk_id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    book_guid         VARCHAR(36) NOT NULL,
    chunk_seq         INT UNSIGNED NOT NULL,
    heading_path      VARCHAR(512) DEFAULT ''
        COMMENT '章节路径，如 "第二章 矿物 > 第三节 常见矿物"',
    char_start        INT UNSIGNED NOT NULL DEFAULT 0
        COMMENT 'chunk 在书 content 内的近似字符偏移',
        -- 历史字段；精确口径见下面三偏移四长度
    char_len          INT UNSIGNED NOT NULL DEFAULT 0,
    visible_start     INT UNSIGNED NOT NULL DEFAULT 0,
    visible_len       INT UNSIGNED NOT NULL DEFAULT 0,
    byte_start        INT UNSIGNED NOT NULL DEFAULT 0,
    byte_len          INT UNSIGNED NOT NULL DEFAULT 0,
    grapheme_len      INT UNSIGNED NOT NULL DEFAULT 0,
    has_pua           TINYINT(1) NOT NULL DEFAULT 0,
        -- 切块政策 v2：三偏移四长度 + PUA 标志（字素簇对齐 / 可见字符口径 / 精确字节偏移）。
        -- 检索侧按 has_pua 决定是否走 SUBSTRING 净化路径。
    content           MEDIUMTEXT NOT NULL,
    content_tokenized MEDIUMTEXT NOT NULL
        COMMENT '应用层 jieba 分词结果，用于 CJK FULLTEXT 搜索',
    embedding         VECTOR(1024) NOT NULL,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_book_seq (book_guid, chunk_seq),
    FULLTEXT idx_ft_chunk_content (content),
    FULLTEXT idx_ft_chunk_tokenized (content_tokenized),
    VECTOR INDEX idx_vec_chunk (embedding) M=16 DISTANCE=cosine,
    CONSTRAINT fk_chunk_book
        FOREIGN KEY (book_guid) REFERENCES documents_book(guid)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;

-- AI 对话
CREATE TABLE IF NOT EXISTS documents_chat (
    guid              VARCHAR(36) PRIMARY KEY,
    bucket            VARCHAR(64) NOT NULL,
    tags              TEXT DEFAULT '',
    key_metadata      MEDIUMTEXT DEFAULT '',        -- 对话摘要
    content           MEDIUMTEXT NOT NULL,
    content_tokenized MEDIUMTEXT NOT NULL            -- jieba 分词
        COMMENT '应用层 jieba 分词结果，用于 CJK FULLTEXT 搜索',
    embedding    VECTOR(1024) NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FULLTEXT idx_ft_content_chat (content),
    FULLTEXT idx_ft_tokenized_chat (content_tokenized),
    VECTOR INDEX idx_vec_chat (embedding) M=16 DISTANCE=cosine
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;

-- OneNote 笔记 + 剪报（无 VECTOR——笔记太短，全文搜索够用）
CREATE TABLE IF NOT EXISTS documents_onenote (
    guid              VARCHAR(36) PRIMARY KEY,
    bucket            VARCHAR(64) NOT NULL,
    tags              TEXT DEFAULT '',
    key_metadata      MEDIUMTEXT DEFAULT '',        -- 标题 or 前50字
    content           MEDIUMTEXT NOT NULL,
    content_tokenized MEDIUMTEXT NOT NULL            -- jieba 分词
        COMMENT '应用层 jieba 分词结果，用于 CJK FULLTEXT 搜索',
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FULLTEXT idx_ft_content_onenote (content),
    FULLTEXT idx_ft_tokenized_onenote (content_tokenized)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;

-- ── 图片 ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS images (
    md5_hash   CHAR(32) PRIMARY KEY,
    file_path  VARCHAR(1024) NOT NULL,
    ref_count  INT UNSIGNED DEFAULT 1,
    ocr_text   MEDIUMTEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 配置表 ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS category_config (
    category          VARCHAR(32) PRIMARY KEY,
    table_name        VARCHAR(64) NOT NULL,
    use_embedding     BOOLEAN DEFAULT TRUE,
    use_rerank        BOOLEAN DEFAULT TRUE,
    key_metadata_desc TEXT DEFAULT '',
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

INSERT IGNORE INTO category_config
    (category, table_name, use_embedding, use_rerank, key_metadata_desc)
VALUES
    ('book',    'documents_book',    TRUE,  TRUE,  '书名（从原始文件名提取）'),
    ('chat',    'documents_chat',    TRUE,  TRUE,  '对话摘要（自动生成）'),
    ('onenote', 'documents_onenote', FALSE, FALSE, '标题优先，否则正文前50个Unicode可见字符');

CREATE TABLE IF NOT EXISTS bucket_config (
    bucket      VARCHAR(64) PRIMARY KEY,
    category    VARCHAR(32) NOT NULL,
    description TEXT DEFAULT '',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category) REFERENCES category_config(category)
) ENGINE=InnoDB;

INSERT IGNORE INTO bucket_config
    (bucket, category, description)
VALUES
    ('图书',       'book',    '正式出版物、论文等'),
    ('游戏剧情',   'book',    '游戏剧本、剧情分析'),
    ('AI对话',     'chat',    '与AI助手的完整对话记录'),
    ('OneNote笔记', 'onenote', '个人笔记、零散想法'),
    ('剪报',       'onenote', '网页剪辑、文章收藏');

-- ── 嵌入缓存 ──────────────────────────────────────────
-- ⚠️ COLLATE=utf8mb4_bin 是刻意的：缓存键按字节比较（模型名大小写敏感），
--    不要跟随库默认的 uca1400_ai_ci。
CREATE TABLE IF NOT EXISTS embedding_cache (
    text_hash   CHAR(32) NOT NULL COMMENT 'md5(utf8 text)',
    model       VARCHAR(128) NOT NULL COMMENT '模型标识（含 local:/remote: 前缀）',
    vector_json MEDIUMTEXT NOT NULL COMMENT 'JSON float 数组',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  DATETIME DEFAULT NULL COMMENT 'NULL = 永不过期',
    PRIMARY KEY (text_hash, model),
    KEY idx_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- ── MinerU 脚注 ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS page_footnotes (
    id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    book_guid      VARCHAR(36) NOT NULL,
    page_idx       INT UNSIGNED NOT NULL,
    footnote_text  MEDIUMTEXT NOT NULL,
    footnote_type  VARCHAR(32) DEFAULT '',
    bbox           VARCHAR(255) DEFAULT '',
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_footnotes_book (book_guid),
    -- 幂等锚点：force 重导时配合 INSERT IGNORE，同一页同位置的脚注不会重复入库
    UNIQUE KEY uq_footnote (book_guid, page_idx, bbox),
    CONSTRAINT fk_footnotes_book
        FOREIGN KEY (book_guid) REFERENCES documents_book(guid)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;
