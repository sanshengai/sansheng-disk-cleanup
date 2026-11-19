import pytest

from test_dashboard_v06 import browser, open_page, sample_payload, pw
from test_report_v06 import env, finish, report


@pw
@pytest.mark.parametrize('mode', ['trash', 'delete'])
def test_confirmation_explains_disposal(browser, tmp_path, mode):
    data = sample_payload()
    data.setdefault('proposal', {})['delete_mode'] = mode
    page, errors = open_page(browser, tmp_path, data)
    hint = page.locator('#selection-help')
    assert hint.is_visible()
    text = hint.inner_text()
    if mode == 'trash':
        assert '废纸篓' in text and '清空后释放空间' in text
        assert '永久删除' not in text
    else:
        assert '永久删除' in text and '不可撤销' in text
        assert '废纸篓' not in text
    assert not errors
    page.close()


@pw
@pytest.mark.parametrize('pending', [True, False])
@pytest.mark.parametrize('amount, expected', [(1_100_000_000, '1.1GB'), (21_000_000, '21.0MB')])
def test_result_headline_and_poster(browser, env, pending, amount, expected):
    finish(env)
    r = report.build_report(str(env.run))
    r['freed'] = 0 if pending else amount
    r['disposal']['trash'] = amount if pending else 0
    path = report.render(str(env.run), r)
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('file://' + path)
    assert page.locator('#freed').inner_text() == expected
    assert page.locator('#headline').inner_text() == ('已移入废纸篓，等待彻底删除' if pending else '按条目核算的清理量')
    if pending:
        assert '按条目核算释放 0 B' in page.locator('#sub').inner_text()
    assert page.locator('#poster').evaluate('(c)=>c.toDataURL().length') > 1000
    assert not errors
    page.close()


@pw
@pytest.mark.parametrize('observed', [True, False])
def test_result_page_distinguishes_physical_loss_from_item_totals(browser, env, observed):
    data = finish(env)
    data.update(freed=5_000_000, disk_total=100_000_000,
                free_before=40_000_000, free_after=30_000_000, used_before=52_000_000,
                used_after=62_000_000, freed_measured=-10_000_000)
    if not observed:
        data.pop('volume_scope')  # Old report.json may carry plausible-looking synthesized fields.
    page = browser.new_page()
    page.goto('file://' + report.render(str(env.run), data))
    assert page.locator('#freed').inner_text() == '5.0MB'
    if observed:
        assert '-10.0 MB' in page.locator('#sub').inner_text()
        assert '62.0 MB' in page.locator('#bars [role=img]').last.get_attribute('aria-label')
    else:
        assert '变化未知' in page.locator('#sub').inner_text()
        assert not page.locator('#bars [role=img]').count()
    page.close()
