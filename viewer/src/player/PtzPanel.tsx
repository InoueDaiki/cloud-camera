// PTZ の操作パネル(ライブ再生中だけ、映像の下に出す)。
//
// - 方向パッドとズームは「押している間 continuous、離したら stop」(短く押したときは一定量だけ relative で動かす)。ポインタが外れたとき、タブが非表示になったとき、
//   ウィンドウからフォーカスが外れたとき、パネルが消えるときも stop を送る
// - キーボードはパネル(またはその中のボタン)にフォーカスがあるときだけ: 矢印で向き、+ / − でズーム、Home でホーム
// - 命令は 1 つずつ順に送る(PtzQueue)。押し続けている間は自動停止(timeout_ms)の前に送り直す
// - 現在の位置は GET .../ptz(シャドウ)。操作中は短い間隔でポーリングする

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react';
import { ActionIcon, Badge, Button, Group, Menu, Popover, Stack, Text, TextInput, Tooltip } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  IconBookmark,
  IconChevronDown,
  IconChevronLeft,
  IconChevronRight,
  IconChevronUp,
  IconDeviceFloppy,
  IconHome,
  IconInfoCircle,
  IconMinus,
  IconPlus,
} from '@tabler/icons-react';
import { api } from '../api/client';
import type { PtzCommand } from '../api/types';
import {
  CONTINUOUS_REFRESH_MS,
  cropRect,
  HOLD_DELAY_MS,
  formatAxis,
  isZero,
  keyToDir,
  moveCommand,
  nextPresetName,
  PtzQueue,
  stepCommand,
  velocityOf,
  type PtzDir,
  type Velocity,
} from '../lib/ptz';
import classes from './PtzPanel.module.css';

/** 操作のあと、この時間は位置を短い間隔で取り直す */
const ACTIVE_MS = 6000;
const POLL_ACTIVE_MS = 700;
const POLL_IDLE_MS = 10_000;

const DIR_LABEL: Record<PtzDir, string> = {
  up: '上へ',
  down: '下へ',
  left: '左へ',
  right: '右へ',
  in: 'ズームイン',
  out: 'ズームアウト',
};

interface Props {
  cameraId: string;
}

export function PtzPanel({ cameraId }: Props) {
  const qc = useQueryClient();
  const coarse = useMediaQuery('(pointer: coarse)', false);
  const btn = coarse ? 'xl' : 'lg';
  const icon = coarse ? 22 : 18;

  const [pressed, setPressed] = useState<ReadonlySet<PtzDir>>(new Set());
  const [active, setActive] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [saveOpen, setSaveOpen] = useState(false);
  const [presetName, setPresetName] = useState('');

  const pointerDir = useRef<PtzDir | null>(null);
  const keys = useRef(new Set<PtzDir>());
  const lastVel = useRef<Velocity>({ pan: 0, tilt: 0, zoom: 0 });
  const refreshTimer = useRef<number | undefined>(undefined);
  const holdTimer = useRef<number | undefined>(undefined);
  const holding = useRef(false);
  const tapDirs = useRef(new Set<PtzDir>());
  const activeTimer = useRef<number | undefined>(undefined);

  const queue = useMemo(
    () =>
      new PtzQueue(
        (cmd) => api.ptzCommand(cameraId, cmd),
        (e) => setSendError(`PTZ の操作を送れませんでした: ${(e as Error).message}`),
      ),
    [cameraId],
  );

  const ptz = useQuery({
    queryKey: ['ptz', cameraId],
    queryFn: () => api.ptz(cameraId),
    refetchInterval: active || pressed.size > 0 ? POLL_ACTIVE_MS : POLL_IDLE_MS,
  });
  const st = ptz.data;

  const send = useCallback(
    (cmd: PtzCommand) => {
      setSendError(null);
      setActive(true);
      window.clearTimeout(activeTimer.current);
      activeTimer.current = window.setTimeout(() => setActive(false), ACTIVE_MS);
      queue.push(cmd);
    },
    [queue],
  );

  // 押している方向(ポインタとキーの和)が変わったら、continuous か stop を送る
  const currentDirs = useCallback(() => {
    const dirs = new Set(keys.current);
    if (pointerDir.current) dirs.add(pointerDir.current);
    return dirs;
  }, []);

  // 押し続けている間の速度を送る(0 なら stop)
  const applyVelocity = useCallback(
    (dirs: Set<PtzDir>) => {
      const v = velocityOf(dirs);
      const prev = lastVel.current;
      if (v.pan === prev.pan && v.tilt === prev.tilt && v.zoom === prev.zoom) return;
      lastVel.current = v;
      window.clearInterval(refreshTimer.current);
      send(moveCommand(v));
      if (isZero(v)) {
        holding.current = false;
      } else {
        refreshTimer.current = window.setInterval(() => send(moveCommand(v)), CONTINUOUS_REFRESH_MS);
      }
    },
    [send],
  );

  // 押している方向(ポインタとキーの和)が変わったら呼ぶ。
  // HOLD_DELAY_MS より短い押下(タップ)は一定量だけ動かす(relative)。それより長ければ押している間 continuous
  const update = useCallback(() => {
    const dirs = currentDirs();
    setPressed(dirs);
    if (holding.current) {
      applyVelocity(dirs);
      return;
    }
    if (dirs.size > 0) {
      dirs.forEach((d) => tapDirs.current.add(d));
      if (holdTimer.current === undefined) {
        holdTimer.current = window.setTimeout(() => {
          holdTimer.current = undefined;
          tapDirs.current.clear();
          holding.current = true;
          applyVelocity(currentDirs());
        }, HOLD_DELAY_MS);
      }
      return;
    }
    if (holdTimer.current !== undefined) {
      window.clearTimeout(holdTimer.current);
      holdTimer.current = undefined;
      const tap = new Set(tapDirs.current);
      tapDirs.current.clear();
      if (tap.size > 0) send(stepCommand(tap));
    }
  }, [applyVelocity, currentDirs, send]);

  const releaseAll = useCallback(() => {
    keys.current.clear();
    pointerDir.current = null;
    update();
  }, [update]);

  // タブが非表示になったら、ウィンドウからフォーカスが外れたら止める
  useEffect(() => {
    const onVis = () => document.hidden && releaseAll();
    document.addEventListener('visibilitychange', onVis);
    window.addEventListener('blur', releaseAll);
    return () => {
      document.removeEventListener('visibilitychange', onVis);
      window.removeEventListener('blur', releaseAll);
    };
  }, [releaseAll]);

  // パネルが消えるとき(録画の再生へ移った、カメラを切り替えた)に動いていたら止める
  useEffect(
    () => () => {
      window.clearInterval(refreshTimer.current);
      window.clearTimeout(activeTimer.current);
      window.clearTimeout(holdTimer.current);
      if (!isZero(lastVel.current)) {
        lastVel.current = { pan: 0, tilt: 0, zoom: 0 };
        queue.clear();
        queue.push({ op: 'stop' });
      }
    },
    [queue],
  );

  const holdProps = (dir: PtzDir) => ({
    className: classes.hold,
    'data-pressed': pressed.has(dir) || undefined,
    onPointerDown: (e: PointerEvent<HTMLButtonElement>) => {
      if (e.button !== 0) return;
      // タッチは押した要素に暗黙にキャプチャされる。外すと、指が外れたときに pointerleave が来る
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        // キャプチャされていなければ何もしない
      }
      pointerDir.current = dir;
      update();
    },
    onPointerUp: () => endPointer(dir),
    onPointerLeave: () => endPointer(dir),
    onPointerCancel: () => endPointer(dir),
    onContextMenu: (e: { preventDefault: () => void }) => e.preventDefault(),
    'aria-label': DIR_LABEL[dir],
  });

  function endPointer(dir: PtzDir) {
    if (pointerDir.current !== dir) return;
    pointerDir.current = null;
    update();
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('input, textarea')) return;
    if (e.key === 'Home') {
      e.preventDefault();
      send({ op: 'home' });
      return;
    }
    const dir = keyToDir(e);
    if (!dir) return;
    e.preventDefault();
    if (e.repeat || keys.current.has(dir)) return;
    keys.current.add(dir);
    update();
  };
  const onKeyUp = (e: KeyboardEvent<HTMLDivElement>) => {
    const dir = keyToDir(e);
    if (!dir || !keys.current.has(dir)) return;
    keys.current.delete(dir);
    update();
  };

  const presets = st?.presets ?? [];
  const openSave = () => {
    setPresetName(nextPresetName(presets.map((p) => p.name)));
    setSaveOpen(true);
  };
  const savePreset = () => {
    const name = presetName.trim();
    if (!name) return;
    send({ op: 'set_preset', preset_name: name.slice(0, 64) });
    setSaveOpen(false);
    window.setTimeout(() => void qc.invalidateQueries({ queryKey: ['ptz', cameraId] }), 1500);
  };

  const pan = st?.pan ?? 0;
  const tilt = st?.tilt ?? 0;
  const zoom = st?.zoom ?? 0;
  const rect = cropRect(pan, tilt, zoom);
  const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
  const error = sendError ?? st?.error ?? (ptz.isError ? `PTZ の状態を取得できませんでした: ${(ptz.error as Error).message}` : null);

  const padButton = (dir: PtzDir, children: ReactNode) => (
    <ActionIcon size={btn} variant="default" {...holdProps(dir)}>
      {children}
    </ActionIcon>
  );

  return (
    <div
      className={classes.panel}
      role="group"
      aria-label="PTZ 操作(フォーカス中は矢印キーで向き、+ / − でズーム、Home でホーム)"
      tabIndex={0}
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          keys.current.clear();
          update();
        }
      }}
    >
      <div className={classes.controls}>
        <div className={classes.pad}>
          <span />
          {padButton('up', <IconChevronUp size={icon} />)}
          <span />
          {padButton('left', <IconChevronLeft size={icon} />)}
          <Tooltip label="ホーム位置へ(Home)">
            <ActionIcon size={btn} variant="default" onClick={() => send({ op: 'home' })} aria-label="ホーム位置へ">
              <IconHome size={icon - 2} />
            </ActionIcon>
          </Tooltip>
          {padButton('right', <IconChevronRight size={icon} />)}
          <span />
          {padButton('down', <IconChevronDown size={icon} />)}
          <span />
        </div>
        <div className={classes.zoom}>
          {padButton('in', <IconPlus size={icon} />)}
          <span className={classes.zoomLabel}>ズーム</span>
          {padButton('out', <IconMinus size={icon} />)}
        </div>
      </div>

      <Stack gap={6} className={classes.info}>
        <Group gap="sm" wrap="nowrap">
          <Tooltip label="映像全体のうち、いま表示している範囲(目安)">
            <div className={classes.minimap} aria-hidden>
              <div
                className={classes.window}
                style={{ left: pct(rect.left), top: pct(rect.top), width: pct(rect.width), height: pct(rect.height) }}
              />
            </div>
          </Tooltip>
          <div className={`${classes.readout} tabular`} aria-label="現在の位置">
            <span>
              <span className={classes.axis}>パン</span>
              {formatAxis(st?.pan)}
            </span>
            <span>
              <span className={classes.axis}>チルト</span>
              {formatAxis(st?.tilt)}
            </span>
            <span>
              <span className={classes.axis}>ズーム</span>
              {formatAxis(st?.zoom, false)}
            </span>
          </div>
          {st?.move_status === 'MOVING' && (
            <Badge size="sm" color="brand" style={{ flex: 'none' }}>
              動作中
            </Badge>
          )}
        </Group>

        <Group gap="xs" wrap="wrap">
          <Menu position="top-start" withinPortal>
            <Menu.Target>
              <Button size="compact-sm" variant="default" leftSection={<IconBookmark size={14} />}>
                プリセット{presets.length > 0 ? `(${presets.length})` : ''}
              </Button>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>プリセットへ移動</Menu.Label>
              {presets.length === 0 && <Menu.Item disabled>まだありません</Menu.Item>}
              {presets.map((p) => (
                <Menu.Item key={p.token} onClick={() => send({ op: 'goto_preset', preset_token: p.token })}>
                  {p.name || `プリセット ${p.token}`}
                </Menu.Item>
              ))}
            </Menu.Dropdown>
          </Menu>
          <Popover opened={saveOpen} onChange={setSaveOpen} position="top-start" withArrow trapFocus>
            <Popover.Target>
              <Button size="compact-sm" variant="subtle" leftSection={<IconDeviceFloppy size={14} />} onClick={openSave}>
                現在位置を保存
              </Button>
            </Popover.Target>
            <Popover.Dropdown>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  savePreset();
                }}
              >
                <Group gap="xs" align="flex-end" wrap="nowrap">
                  <TextInput
                    size="xs"
                    label="プリセット名"
                    value={presetName}
                    onChange={(e) => setPresetName(e.currentTarget.value)}
                    maxLength={64}
                    w={180}
                    data-autofocus
                  />
                  <Button size="xs" type="submit" disabled={!presetName.trim()}>
                    保存
                  </Button>
                </Group>
              </form>
            </Popover.Dropdown>
          </Popover>
        </Group>

        <Text size="xs" c="dimmed" className={classes.hint}>
          <IconInfoCircle size={14} className={classes.hintIcon} />
          操作はおよそ 1 秒で映像に反映されます(WebRTC のライブ)
        </Text>
        {error && (
          <Text size="xs" c="red">
            {error}
          </Text>
        )}
      </Stack>
    </div>
  );
}
