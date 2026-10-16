import pytest

from test_dashboard_v06 import browser, open_page, sample_payload, pw
from test_report_v06 import env, finish, report


@pw
@pytest.mark.parametrize('mode', ['trash', 'delete'])
def test_confirmation_explains_disposal(browser, tmp_path, mode):
    data = sample_payload()
    data.setdefault('proposal', {})['delete_mode'] = mode
    page, errors = open_page(browser, tmp_path, data)
    text = page.locator('#selection-help').inner_text()
    assert ('本轮不会立即释放空间' if mode == 'trash' else '直接永久删除') in text
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
    assert page.locator('#headline').inner_text() == ('已移入废纸篓，等待彻底删除' if pending else '这次清理腾出了')
    if pending:
        assert '实际释放仍为 0 B' in page.locator('#sub').inner_text()
    assert page.locator('#poster').evaluate('(c)=>c.toDataURL().length') > 1000
    assert not errors
    page.close()
