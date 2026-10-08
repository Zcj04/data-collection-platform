"""Exercise actual payment rendering and password dialog with offline fixtures."""
import json
from check_responsive import OUTPUT, respond
from playwright.sync_api import sync_playwright


def main():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width in (360, 390, 768, 920, 1366, 1920):
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", respond)
            page.goto("http://layout.test/admin")
            page.wait_for_timeout(400)
            for state in ("missing", "complete", "attention"):
                page.evaluate("""state => {
                    showSection('paymentdata');paSetTab('month');
                }""", state)
                page.wait_for_timeout(150)
                page.evaluate("""state => paRenderMonth({status:state,month_end_date:'2026-08-31',
                    info:state==='missing'?null:{source_file:'合成测试.xlsx'},
                    summary:{raw_total:0,raw_count:state==='missing'?0:40,mapped_count:state==='missing'?0:40},
                    rows:state==='missing'?[]:Array.from({length:40},(_,i)=>({
                        venue:'门店名称很长的测试门店'.repeat(5)+i,owner:'负责人'.repeat(12),
                        operating:true,data_status:i%2?'missing':'present',amount:0,
                        source_names:['原始门店名'.repeat(12)]})),
                    unmatched:state==='attention'?[{shop_name:'未匹配名称'.repeat(40),amount:0}]:[]
                })""", state)
                total = page.locator('#pa-month-kpis strong').first.inner_text()
                assert total == ('—' if state == 'missing' else '¥ 0.00'), total
                results.append({"width": width, "state": state,
                    "overflow": page.evaluate("document.documentElement.scrollWidth > innerWidth+1"),
                    "errors": errors[:],
                    "kpis": page.locator('#pa-month-kpis').inner_text()})
            page.evaluate("showSection('users');openResetPassword('fixture',{username:'long'.repeat(80),display_name:'测试'})")
            page.wait_for_timeout(100)
            dialog = page.locator('#user-reset-dialog')
            bounds = dialog.bounding_box()
            page.keyboard.press('Escape')
            results.append({"width":width,"state":"password-dialog","overflow":bounds['x']<0 or bounds['x']+bounds['width']>width+1,
                            "escape_closed":not dialog.is_visible(),"errors":errors[:]})
            page.close()
        browser.close()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'payment-content.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    failures=[r for r in results if r['overflow'] or r['errors'] or r.get('escape_closed') is False]
    print(json.dumps({"cases":len(results),"failures":failures},ensure_ascii=False))
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
