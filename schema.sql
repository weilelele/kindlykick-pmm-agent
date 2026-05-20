-- Feishu PM Agent — Supabase Schema
-- 在 Supabase SQL Editor 中运行此文件

-- 团队成员
CREATE TABLE members (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feishu_user_id TEXT UNIQUE NOT NULL,  -- 飞书 open_id
    name        TEXT NOT NULL,
    bio         TEXT,                     -- 自我介绍（角色/职责描述）
    is_owner    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Migration: add bio column if upgrading from older schema
-- ALTER TABLE members ADD COLUMN IF NOT EXISTS bio TEXT;

-- 任务
CREATE TABLE tasks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title         TEXT NOT NULL,
    description   TEXT,
    assignee_id   UUID REFERENCES members(id) ON DELETE SET NULL,
    assignee_name TEXT,
    status        TEXT DEFAULT 'pending'
                  CHECK (status IN ('pending', 'in_progress', 'done', 'blocked')),
    priority      TEXT DEFAULT 'normal'
                  CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    due_date      DATE,
    source        TEXT CHECK (source IN ('meeting', 'group_chat', 'private_chat')),
    source_ref    TEXT,   -- 会议名称或消息 ID
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- 进展记录
CREATE TABLE progress_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id     UUID REFERENCES tasks(id) ON DELETE SET NULL,
    member_id   UUID REFERENCES members(id) ON DELETE SET NULL,
    member_name TEXT NOT NULL,
    content     TEXT NOT NULL,
    source      TEXT CHECK (source IN ('group_chat', 'private_chat')),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 会议记录存档
CREATE TABLE meetings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT,
    raw_content     TEXT NOT NULL,
    summary         TEXT,
    tasks_extracted INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_tasks_status     ON tasks(status);
CREATE INDEX idx_tasks_assignee   ON tasks(assignee_name);
CREATE INDEX idx_tasks_updated    ON tasks(updated_at DESC);
CREATE INDEX idx_progress_created ON progress_logs(created_at DESC);
CREATE INDEX idx_members_open_id  ON members(feishu_user_id);
