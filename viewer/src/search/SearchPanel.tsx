// 自然言語検索: 質問 → 回答、該当時刻の一覧(クリックで再生)、生成された SQL とトークン数

import { useState, type FormEvent } from 'react';
import {
  Accordion,
  ActionIcon,
  Alert,
  Badge,
  Code,
  Group,
  Loader,
  Paper,
  Stack,
  Table,
  Text,
  Textarea,
  Title,
  UnstyledButton,
} from '@mantine/core';
import { useMutation } from '@tanstack/react-query';
import { IconAlertCircle, IconArrowUp, IconPlayerPlay, IconSparkles } from '@tabler/icons-react';
import { api } from '../api/client';
import type { Camera, SearchResponse } from '../api/types';
import { formatDateTime, parseTime } from '../lib/time';
import classes from './SearchPanel.module.css';

const EXAMPLES = ['昨日の夕方に玄関に来た人は?', '昨日、駐車場に車は何回来た?'];

// COCO のクラス名の日本語(よく出るものだけ)
const LABELS: Record<string, string> = {
  person: '人',
  car: '車',
  truck: 'トラック',
  bicycle: '自転車',
  motorcycle: 'バイク',
  bus: 'バス',
  dog: '犬',
  cat: '猫',
  bird: '鳥',
  backpack: 'リュック',
  handbag: 'バッグ',
  umbrella: '傘',
};

const USAGE_LABELS: Record<string, string> = {
  input_tokens: '入力トークン',
  output_tokens: '出力トークン',
  total_tokens: '合計トークン',
  athena_bytes_scanned: 'Athena のスキャン量',
  model_id: 'モデル',
  bedrock_calls: 'Bedrock の呼び出し',
  tool_calls: 'ツールの呼び出し',
  elapsed_ms: '所要時間',
};

function formatUsage(key: string, v: unknown): string {
  if (typeof v !== 'number') return v === null || v === undefined ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);
  if (/_ms$/.test(key)) return `${(v / 1000).toFixed(1)} 秒`;
  if (/bytes/.test(key)) return v >= 1024 * 1024 ? `${(v / 1024 / 1024).toFixed(1)} MB` : `${(v / 1024).toFixed(1)} KB`;
  return v.toLocaleString('ja-JP');
}

interface Props {
  cameras: Camera[] | undefined;
  onJump: (cameraId: string, at: number) => void;
}

export function SearchPanel({ cameras, onJump }: Props) {
  const [question, setQuestion] = useState('');
  const [asked, setAsked] = useState<string | null>(null);
  const mutation = useMutation({ mutationFn: (q: string) => api.search(q) });

  const submit = (q: string) => {
    const text = q.trim();
    if (!text || mutation.isPending) return;
    setQuestion(text);
    setAsked(text);
    mutation.mutate(text);
  };
  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit(question);
  };
  const nameOf = (id: string) => cameras?.find((c) => c.camera_id === id)?.name ?? id;

  return (
    <Stack gap="sm">
      <Group gap={6}>
        <IconSparkles size={18} color="var(--mantine-primary-color-filled)" />
        <Title order={2}>検索</Title>
      </Group>
      <form onSubmit={onSubmit}>
        <Textarea
          value={question}
          onChange={(e) => setQuestion(e.currentTarget.value)}
          placeholder="例: 昨日の夕方に玄関に来た人は?"
          autosize
          minRows={2}
          maxRows={5}
          aria-label="質問"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit(question);
            }
          }}
          rightSection={
            <ActionIcon
              type="submit"
              variant="filled"
              color="brand"
              radius="xl"
              disabled={!question.trim()}
              loading={mutation.isPending}
              aria-label="質問する"
            >
              <IconArrowUp size={16} />
            </ActionIcon>
          }
          rightSectionProps={{ style: { alignItems: 'flex-end', paddingBottom: 6 } }}
        />
      </form>
      {!asked && (
        <Group gap={6}>
          {EXAMPLES.map((q) => (
            <UnstyledButton key={q} className={classes.example} onClick={() => submit(q)}>
              {q}
            </UnstyledButton>
          ))}
        </Group>
      )}

      {mutation.isPending && (
        <Group gap="xs" c="dimmed">
          <Loader size="xs" />
          <Text size="sm">検出結果を検索しています…</Text>
        </Group>
      )}
      {mutation.isError && (
        <Alert color="red" icon={<IconAlertCircle size={18} />} title="検索できませんでした" p="sm">
          {(mutation.error as Error).message}
        </Alert>
      )}
      {mutation.data && !mutation.isPending && (
        <Result res={mutation.data} question={asked} nameOf={nameOf} onJump={onJump} />
      )}
    </Stack>
  );
}

function Result({
  res,
  question,
  nameOf,
  onJump,
}: {
  res: SearchResponse;
  question: string | null;
  nameOf: (id: string) => string;
  onJump: (cameraId: string, at: number) => void;
}) {
  const usage = Object.entries(res.usage ?? {});
  return (
    <Stack gap="sm">
      <Paper p="sm" radius="md" className={classes.answer}>
        {question && (
          <Text size="xs" c="dimmed" mb={4}>
            {question}
          </Text>
        )}
        <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
          {res.answer}
        </Text>
      </Paper>

      {res.hits.length > 0 && (
        <div>
          <Text size="xs" c="dimmed" mb={4}>
            該当 {res.hits.length} 件(クリックでその時刻から再生)
          </Text>
          <Stack gap={4}>
            {res.hits.map((h, i) => {
              const at = parseTime(h.ts);
              return (
                <UnstyledButton key={`${h.camera_id}-${h.ts}-${i}`} className={classes.hit} onClick={() => onJump(h.camera_id, at)}>
                  <IconPlayerPlay size={14} className={classes.hitIcon} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Text size="sm" className="tabular" fw={500}>
                      {formatDateTime(at)}
                    </Text>
                    <Text size="xs" c="dimmed" truncate>
                      {nameOf(h.camera_id)}
                    </Text>
                  </div>
                  <Badge color="gray" variant="light">
                    {LABELS[h.label] ?? h.label}
                  </Badge>
                  <Text size="xs" c="dimmed" className="tabular" w={34} ta="right">
                    {Math.round(h.confidence * 100)}%
                  </Text>
                </UnstyledButton>
              );
            })}
          </Stack>
        </div>
      )}

      {(res.sql || usage.length > 0) && (
        <Accordion variant="contained" radius="md" chevronPosition="right">
          {res.sql && (
            <Accordion.Item value="sql">
              <Accordion.Control>
                <Text size="sm">生成された SQL</Text>
              </Accordion.Control>
              <Accordion.Panel>
                <Code block className={classes.sql}>
                  {res.sql}
                </Code>
              </Accordion.Panel>
            </Accordion.Item>
          )}
          {usage.length > 0 && (
            <Accordion.Item value="usage">
              <Accordion.Control>
                <Text size="sm">トークン数・スキャン量</Text>
              </Accordion.Control>
              <Accordion.Panel>
                <Table withRowBorders={false} verticalSpacing={2} fz="sm">
                  <Table.Tbody>
                    {usage.map(([k, v]) => (
                      <Table.Tr key={k}>
                        <Table.Td c="dimmed">{USAGE_LABELS[k] ?? k}</Table.Td>
                        <Table.Td ta="right" className="tabular">
                          {formatUsage(k, v)}
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Accordion.Panel>
            </Accordion.Item>
          )}
        </Accordion>
      )}
    </Stack>
  );
}
