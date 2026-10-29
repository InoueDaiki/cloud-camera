import { useCallback, useEffect, useState } from 'react';
import {
  ActionIcon,
  AppShell,
  Badge,
  Burger,
  Group,
  Menu,
  ScrollArea,
  Text,
  Title,
  Tooltip,
  useMantineColorScheme,
} from '@mantine/core';
import { useDisclosure, useLocalStorage, useMediaQuery } from '@mantine/hooks';
import { useQuery } from '@tanstack/react-query';
import { IconDeviceDesktop, IconMoon, IconSun, IconVideo } from '@tabler/icons-react';
import { api } from './api/client';
import { isCodecSwitching } from './lib/codec';
import { isEncoderApplying } from './lib/encoder';
import { CameraList } from './cameras/CameraList';
import { Player } from './player/Player';
import { usePlayback } from './player/usePlayback';
import { SearchPanel } from './search/SearchPanel';
import { parseTime } from './lib/time';

function useNow(intervalMs = 5000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function App() {
  const [navOpened, nav] = useDisclosure(false);
  const wide = useMediaQuery('(min-width: 75em)', true);
  const now = useNow();
  const [savedCamera, setSavedCamera] = useLocalStorage<string | null>({ key: 'cc-camera', defaultValue: null });

  // 映像形式の切り替え中とエンコーダーの設定の反映中は、終わったことがすぐ分かるように 3 秒ごとに取り直す
  const cameras = useQuery({
    queryKey: ['cameras'],
    queryFn: api.cameras,
    refetchInterval: (q) =>
      q.state.data?.cameras.some((c) => isCodecSwitching(c) || isEncoderApplying(c)) ? 3_000 : 15_000,
  });
  const list = cameras.data?.cameras;

  const processedUntil = useCallback(
    (id: string) => {
      const c = list?.find((x) => x.camera_id === id);
      return c?.video_processed_until ? parseTime(c.video_processed_until) : null;
    },
    [list],
  );
  const pb = usePlayback(processedUntil);
  const { open } = pb;

  // 最初のカメラ(前回見ていたカメラ)をライブで開く
  useEffect(() => {
    if (pb.cameraId || !list?.length) return;
    const first = list.find((c) => c.camera_id === savedCamera) ?? list[0];
    open(first.camera_id, { kind: 'live' });
  }, [list, pb.cameraId, savedCamera, open]);

  const selectCamera = (id: string) => {
    setSavedCamera(id);
    nav.close();
    open(id, { kind: 'live' });
  };
  const jump = (id: string, at: number) => {
    setSavedCamera(id);
    open(id, { kind: 'time', at });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const camera = list?.find((c) => c.camera_id === pb.cameraId);
  const search = <SearchPanel cameras={list} onJump={jump} />;

  return (
    <AppShell
      header={{ height: 52 }}
      navbar={{ width: 300, breakpoint: 'sm', collapsed: { mobile: !navOpened } }}
      aside={{ width: 360, breakpoint: 'lg', collapsed: { mobile: true, desktop: !wide } }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Burger opened={navOpened} onClick={nav.toggle} hiddenFrom="sm" size="sm" aria-label="カメラ一覧" />
            <IconVideo size={22} color="var(--mantine-primary-color-filled)" />
            <Title order={1} size="h2" style={{ whiteSpace: 'nowrap' }}>
              クラウドカメラ
            </Title>
            {import.meta.env.VITE_MOCK === 'true' && (
              <Tooltip label="API はモック(MSW)です。AWS には接続していません">
                <Badge color="grape" variant="light">
                  モック
                </Badge>
              </Tooltip>
            )}
          </Group>
          <ColorSchemeMenu />
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="md">
        <AppShell.Section>
          <Text size="xs" fw={600} c="dimmed" mb="xs">
            カメラ
          </Text>
        </AppShell.Section>
        <AppShell.Section grow component={ScrollArea} type="auto">
          <CameraList
            cameras={list}
            loading={cameras.isLoading}
            error={cameras.error}
            selectedId={pb.cameraId}
            now={now}
            onSelect={selectCamera}
          />
        </AppShell.Section>
      </AppShell.Navbar>

      <AppShell.Main>
        <Player pb={pb} camera={camera} now={now} />
        {!wide && (
          <div style={{ marginTop: 'var(--mantine-spacing-xl)' }}>
            {search}
          </div>
        )}
      </AppShell.Main>

      {wide && (
        <AppShell.Aside p="md">
          <AppShell.Section grow component={ScrollArea} type="auto" offsetScrollbars>
            {search}
          </AppShell.Section>
        </AppShell.Aside>
      )}
    </AppShell>
  );
}

function ColorSchemeMenu() {
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const icon = colorScheme === 'dark' ? <IconMoon size={18} /> : colorScheme === 'light' ? <IconSun size={18} /> : <IconDeviceDesktop size={18} />;
  const items = [
    ['auto', '端末の設定に合わせる', <IconDeviceDesktop size={16} key="a" />],
    ['light', 'ライト', <IconSun size={16} key="l" />],
    ['dark', 'ダーク', <IconMoon size={16} key="d" />],
  ] as const;
  return (
    <Menu position="bottom-end" withinPortal>
      <Menu.Target>
        <ActionIcon size="lg" aria-label="表示テーマ">
          {icon}
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>表示テーマ</Menu.Label>
        {items.map(([value, label, ic]) => (
          <Menu.Item key={value} leftSection={ic} onClick={() => setColorScheme(value)} fw={colorScheme === value ? 600 : undefined}>
            {label}
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}
