#!/bin/env python3

import asyncio
import inspect
import os
from pathlib import Path

from rich import box
from rich.panel import Panel
from rich.pretty import Pretty
from rich.progress import Progress, TextColumn, BarColumn, DownloadColumn, TransferSpeedColumn, SpinnerColumn, ProgressColumn
from rich.table import Table, Column
from rich.text import Text

from textual import events
from textual.app import App
from textual.message import Message
from textual.views import GridView, DockView
from textual.widgets import Header, Footer, Placeholder, ScrollView
from textual.widget import Widget, Reactive


from .tg import TgClient, TgException, TgVideo


CURSOR_LEFT = ':point_right:'
CURSOR_RIGHT = ':point_left:'
CURSOR = ':point_right:'
FOCUSED_STYLE = 'green'
UNFOCUSED_STYLE = 'white'

RUNNING = ':green_circle:'
STOPPED = ':red_circle:'


class VideoSelected(Message):
    def __init__(self, sender, video, start: bool = True):
        super().__init__(sender)
        self.video = video
        self.start = start


class VideoDownloaded(Message):
    def __init__(self, sender, video):
        super().__init__(sender)
        self.video = video


class VideoPointed(Message):
    def __init__(self, sender, video):
        super().__init__(sender)
        self.video = video


class FilterChanged(Message):
    def __init__(self, sender, video_filter):
        super().__init__(sender)
        self.video_filter = video_filter


class FocusableMixin:
    can_focus = True
    has_focus: Reactive[bool] = Reactive(False)
    
    def on_focus(self):
        self.has_focus = True
        self.log(self.app.focusables)

    def on_blur(self):
        self.has_focus = False


class ScrollableListMixin:
    focused_index: Reactive[int] = Reactive(None)
    render_offset = 0

    @property
    def available_height(self):
        raise NotImplementedError

    @property
    def items(self):
        raise NotImplementedError

    @property
    def focused_item(self):
        if self.focused_index is None or self.focused_index >= len(self.items):
            return None

        return self.items[self.focused_index]

    async def key_down(self, event):
        self._scroll(1)

    async def key_up(self, event):
        self._scroll(-1)

    async def key_pagedown(self, event):
        self._scroll(self.available_height)

    async def key_pageup(self, event):
        self._scroll(-self.available_height)

    async def key_home(self, event):
        if not self.items:
            return

        self.render_offset = 0
        self.focused_index = 0

    async def key_end(self, event):
        if not self.items:
            return

        self._check_offset(len(self.items) - 1)
        self.focused_index = len(self.items) - 1

    async def on_mouse_scroll_up(self):
        await self.key_down(None)

    async def on_mouse_scroll_down(self):
        await self.key_up(None)

    async def key_escape(self, event):
        self.focused_index = None

    def _scroll(self, delta):
        if not self.items:
            self.focused_index = None
        elif self.focused_item not in self.items:
            self.render_offset = 0
            self.focused_index = 0
        else:
            ln = len(self.items)
            new = (ln + self.focused_index + delta) % ln
            self._check_offset(new)
            self.focused_index = new

    def _check_offset(self, pos):
        end = self.render_offset + self.available_height - 1
        if pos > end:
            self.render_offset = pos - self.available_height + 1
        elif pos < self.render_offset:
            self.render_offset = pos

    async def on_click(self, event):
        if event.y in (0, self.size.height - 1):
            return

        index = event.y + self.render_offset - 1
        if index < len(self.items):
            self.focused_index = index

    @classmethod
    def with_focused_item(cls, func):
        if inspect.iscoroutinefunction(func):
            async def wrapped(self, *args, **kwargs):
                if self.focused_item is not None:
                    return await func(self, *args, **kwargs)
        else:
            def wrapped(self, *args, **kwargs):
                if self.focused_item is not None:
                    return func(self, *args, **kwargs)
        return wrapped


class ShortDownload(ProgressColumn):
    def render(self, task):
        mb = 1024 * 1024
        completed = task.completed // mb
        total = task.total // mb
        return Text(f'{completed}/{total}', style='progress.download')


class DownloadList(FocusableMixin, ScrollableListMixin, Widget):
    def __init__(self, tg):
        super().__init__()

        self.tg = tg
        tg.add_download_callback(self._video_updated)

        self.progress = Progress(
          TextColumn('{task.fields[focus_left]}',
                     table_column=Column(width=2)),
          TextColumn('{task.fields[state]}',
                     table_column=Column(width=2)),
          SpinnerColumn(),
          TextColumn('{task.description:.7}',
                     table_column=Column(no_wrap=True)),
          BarColumn(bar_width=8),
          ShortDownload(), # 9
          TransferSpeedColumn(),  # 10
          TextColumn('{task.fields[focus_right]}',
                     table_column=Column(width=2)),
          expand=True,
        )

    def __del__(self):
        self.tg.remove_download_callback(self._video_updated)

    @property
    def available_height(self):
        return self.size.height - 2

    @property
    def items(self):
        return self.progress.task_ids

    def _find_task_by_id(self, id_):
        for task in self.progress.tasks:
            if task.id == id_:
                return task

    def _find_task_by_video(self, video):
        for task in self.progress.tasks:
            if task.fields['video'] == video:
                return task

    async def _start_task(self, task):
        self.tg.download_video(task.fields['video'])
        self.progress.start_task(task.id)
        self.progress.update(task.id, running=True)

    async def _stop_task(self, task):
        self.tg.cancel_download_video(task.fields['video'])
        self.progress.stop_task(task.id)
        self.progress.update(task.id, running=False)

    def _video_updated(self, video):
        task = self._find_task_by_video(video)
        if not task:
            return
        self.progress.update(task.id, completed=video.downloaded_size)

    async def _check(self):
        for task in self.progress.tasks[:]:
            video = task.fields['video']
            if video.completed:
                await self.emit(VideoDownloaded(self, task.fields['video']))
                self.progress.remove_task(task.id)
                self._scroll(0)

        self.refresh()

    async def on_mount(self, event):
        self.set_interval(0.25, callback=self._check)

        for video in self.tg.videos:
            if video.downloaded_size:
                self.add_video(video, start=False)

    def render(self):
        for task in self.progress.tasks:
            if task.id == self.focused_item:
                focus = (CURSOR_LEFT, CURSOR_RIGHT)
            else:
                focus = ('', '')
            task.fields['focus_left'], task.fields['focus_right'] = focus
            task.fields['state'] = RUNNING if task.fields['running'] else STOPPED
            # todo: scroll
        return Panel(
            self.progress,
            title=f'downloads ({len(self.progress.task_ids)})',
            border_style=FOCUSED_STYLE if self.has_focus else UNFOCUSED_STYLE,
        )

    async def on_key(self, event):
        if event.key == ' ':
            await self.key_space(event)
        else:
            await self.dispatch_key(event)

    @ScrollableListMixin.with_focused_item
    async def key_space(self, event):
        task = self._find_task_by_id(self.focused_item)
        if not task:
            return
        if task.finished:
            return

        if task.fields['running']:
            await self._stop_task(task)
        else:
            await self._start_task(task)

        self.refresh()

    @ScrollableListMixin.with_focused_item
    async def key_d(self, event):
        task = self._find_task_by_id(self.focused_item)
        video = task.fields['video']
        self.tg.delete_video(video)
        self.progress.remove_task(task.id)
        self._scroll(0)
        self.refresh()

    @ScrollableListMixin.with_focused_item
    async def key_o(self, event):
        task = self._find_task_by_id(self.focused_item)
        video = task.fields['video']

        if video.completed:
            os.system(f"termux-open '{video.local_path}'")

    async def handle_video_selected(self, event):
        video = event.video
        self.add_video(video)

    def add_video(self, video, start=True):
        task = self._find_task_by_video(video)
        if task:
            return

        self.progress.add_task(
            video.caption.splitlines()[0] if video.caption else '',
            total=video.expected_size,
            completed=video.downloaded_size,
            start=start,
            video=video,
            running=start,
        )

        if start:
            self.tg.download_video(video)

    async def watch_focused_index(self, value):
        if value is None:
            await self.emit(VideoPointed(self, None))
            return

        task = self._find_task_by_id(self.focused_item)
        await self.emit(VideoPointed(self, task.fields['video']))


class VideoList(FocusableMixin, ScrollableListMixin, Widget):
    def __init__(self, tg):
        super().__init__()
        self.tg = tg
        self.video_filter = lambda v: True

    def render(self):
        cap_w = self.size.width - 6 - 6 - 2 - 2 - 4*2 - 2*2 - 2
        t = Table(
            Column('', width=2),
            Column('caption', width=cap_w, no_wrap=True),
            Column(':arrow_up:  dur', width=6, justify='right'),
            Column(':arrow_down: size', width=6, justify='right'),
            Column('', width=2),
            box=None,
        )

        for v in self.items[self.render_offset:self.render_offset+self.available_height]:
            caption = v.caption.splitlines()[0] if v.caption else ''
            color = ''
            if v.completed:
                color = '[green]'
            elif v.downloaded_size:
                color = '[yellow]'
            duration = f'{int(v.duration/60):02}:{v.duration%60:02}'
            size = f'{int(v.expected_size / 1024**2)}mb'
            l, r = (CURSOR_LEFT, CURSOR_RIGHT) if v is self.focused_item else ('', '')
            t.add_row(l, color + caption, color + duration, color + size, r)

        suffix = ''
        if self.focused_item:
            suffix = f' / {self.focused_index + 1}'
        return Panel(
            t,
            title=f'videos ({len(self.tg.videos)} / {len(self.items)}{suffix})',
            border_style=FOCUSED_STYLE if self.has_focus else UNFOCUSED_STYLE,
        )

    @property
    def available_height(self):
        return self.size.height - 3

    @property
    def items(self):
        return [v for v in self.tg.videos if self.video_filter(v)]

    async def on_key(self, event):
        await self.dispatch_key(event)

    @ScrollableListMixin.with_focused_item
    async def key_enter(self, event):
        await self.emit(VideoSelected(self, self.focused_item))

    @ScrollableListMixin.with_focused_item
    async def key_d(self, event):
        item = self.focused_item
        await self._delete(item)

    async def _delete(self, item):
        # todo
        # if not item.completed:
        #     self.tg.delete_video(item)
        self.tg.delete_message(item)
        self._scroll(0)
        self.refresh()

    @ScrollableListMixin.with_focused_item
    async def key_m(self, event):
        item = self.focused_item
        await self._move(item)

    async def _move(self, item):
        if not item.completed:
            return
        p = Path(item.local_path)
        p.rename(p.parents[3] / p.name)
        await self._delete(item)

    async def key_r(self, event):
        # self.tg.reset()
        self.tg.list_videos()
        self.refresh()

    async def key_n(self, event):
        self.tg.load_next()
        self.refresh()

    async def key_s(self, event):
        ...  # todo: sort

    async def watch_focused_index(self, index):
        await self.emit(VideoPointed(self, self.focused_item))

    async def handle_filter_changed(self, event):
        self.video_filter = event.video_filter
        self.refresh()

    async def handle_video_downloaded(self, event):
        await self._move(event.video)


class VideoInfo(Widget):
    video = None

    def render(self):
        return Panel(
            Pretty(self.video) if self.video else '',
            title='info',
        )

    async def handle_video_pointed(self, event):
        self.video = event.video
        self.refresh()


class VideoFilter(ScrollableListMixin, Widget):
    filters = {
        'max duration': [False, '15', 'minuts'],
        'min duration': [False, '1', 'minuts'],
        'max size': [False, '', 'mb'],
        'pinned': [False, '', ''],
    }

    @property
    def items(self):
        return list(self.filters)

    @property
    def available_height(self):
        return self.size.height - 2

    async def on_key(self, event):
        if event.key == ' ':
            await self.key_space(event)
        elif event.key == 'ctrl+h':
            await self.key_backspace(event)
        elif event.key.isdigit():
            await self.key_number(event)
        else:
            await self.dispatch_key(event)

    @ScrollableListMixin.with_focused_item
    async def key_space(self, event):
        attrs = self.filters[self.focused_item]
        attrs[0] = not attrs[0]
        self.refresh()

    async def key_enter(self, event):
        max_duration = 0
        if self.filters['max duration'][0] and self.filters['max duration'][1]:
            max_duration = int(self.filters['max duration'][1]) * 60

        min_duration = 0
        if self.filters['min duration'][0] and self.filters['min duration'][1]:
            min_duration = int(self.filters['min duration'][1]) * 60

        max_size = 0
        if self.filters['max size'][0] and self.filters['max size'][1]:
            max_size = int(self.filters['max size'][1]) * 1024 ** 2

        pinned = self.filters['pinned'][0]

        def video_filter(video):
            if max_duration and video.duration > max_duration:
                return False
            if min_duration and video.duration < min_duration:
                return False
            if max_size and video.expected_size > max_size:
                return False
            if pinned:
                ...
                # todo
                # if not video.pinned:
                #     return False
            return True

        await self.emit(FilterChanged(self, video_filter))

    async def key_escape(self, event):
        await self.emit(FilterChanged(self, None))

    async def key_r(self, event):
        for attrs in self.filters.values():
            attrs[0] = False
            attrs[1] = ''
        self.refresh()

    @ScrollableListMixin.with_focused_item
    async def key_backspace(self, event):
        attrs = self.filters[self.focused_item]
        attrs[1] = attrs[1][:-1]
        self.refresh()

    @ScrollableListMixin.with_focused_item
    async def key_number(self, event):
        attrs = self.filters[self.focused_item]
        if len(attrs[1]) > 4:
            return

        attrs[1] += event.key
        self.refresh()

    def render(self):
        w = self.size.width - 2 - 4*2 - 2*2 - 22
        t = Table(
            Column('', width=2),
            Column('name', width=w),
            Column('enabled', width=3),
            Column('value', width=15),
            Column('', width=2),
            show_header=False,
            box=None,
        )
        for name, attrs in self.filters.items():
            l, r = (CURSOR_LEFT, CURSOR_RIGHT) if self.focused_item == name else ('', '')
            check = ':white_check_mark:' if attrs[0] else ':cross_mark:'
            t.add_row(l, name, check, attrs[1] + ' ' + attrs[2], r)

        return Panel(
            t,
            title='filters',
        )


class MyApp(App):
    async def on_load(self, event: events.Load) -> None:
        await self.bind('q', 'quit', 'quit')
        await self.bind('tab', 'next_tab', 'next tab')
        await self.bind('ctrl+i', 'next_tab', show=False)
        await self.bind('f', 'filter', 'filters')

    async def on_mount(self, event: events.Mount) -> None:
        self.main_page = GridView()
        await self.view.dock(self.main_page)
        grid = self.main_page.grid
        grid.add_column('left')
        grid.add_column('right')
        grid.add_row('top', size=10)
        grid.add_row('middle')
        grid.add_row('bottom')
        grid.add_row('footer', size=1)

        grid.add_areas(
            top='left-start|right-end,top',
            mid='left-start|right-end,middle',
            bottom='left-start|right-end,bottom',
            left='left,bottom',
            right='right,bottom',
            footer='left-start|right-end,footer',
        )

        tg = TgClient()
        # todo: in bg
        tg.init()
        tg.list_videos()

        self.download_list = DownloadList(tg)
        self.video_list = VideoList(tg)
        self.video_info = VideoInfo()
        self.footer = Footer()

        self.focusables = [self.download_list, self.video_list]

        grid.place(
            top=self.download_list,
            mid=self.video_list,
            bottom=self.video_info,
            footer=self.footer,
        )

        self.second_page = DockView()
        self.second_page.visible = False
        self.video_filter = VideoFilter()
        await self.second_page.dock(self.video_filter)
        await self.view.dock(self.second_page)

    async def action_next_tab(self):
        if not self.main_page.visible:
            return

        if self.focused not in self.focusables:
            await self.set_focus(self.focusables[0])
            return

        index = self.focusables.index(self.focused)
        next_index = (index + 1) % len(self.focusables)
        await self.set_focus(self.focusables[next_index])

    async def action_filter(self):
        self.main_page.visible = not self.main_page.visible
        self.second_page.visible = not self.second_page.visible
        if self.second_page.visible:
            await self.set_focus(self.video_filter)
        self.refresh(layout=True)

    async def handle_video_selected(self, event):
        event.stop()
        await self.download_list.forward_event(event)

    async def handle_video_downloaded(self, event):
        event.stop()
        await self.video_list.forward_event(event)

    async def handle_video_pointed(self, event):
        event.stop()
        await self.video_info.forward_event(event)

    async def handle_filter_changed(self, event):
        event.stop()
        await self.action_filter()
        if event.video_filter:
            await self.video_list.forward_event(event)


def main():
    MyApp.run(title='Tg loader', log='log')


if __name__ == '__main__':
    main()
