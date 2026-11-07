from pathlib import Path
import json
import pandas as pd,streamlit as st
from fundamentallab.byma import BymaClient,build_argentina_contexts
from fundamentallab.criteria import CRITERIA,MIN_CRITERIA_TO_SURFACE,QUARTERS_TO_ANALYZE,DCF_PROJECTION_YEARS,DCF_DISCOUNT_RATE,DCF_TERMINAL_GROWTH,DCF_GROWTH_CAP,INTRINSIC_MARGIN_OF_SAFETY
from fundamentallab.database import connect,create_run,finish_run,latest_results,latest_run_stats,company_count,latest_market_watchlist,latest_technical_results,latest_technical_run
from fundamentallab.presentation import parse_criteria_json
from fundamentallab.scanner import scan_usa,scan_argentina
from fundamentallab.sec_frames import SecFramesClient,build_us_contexts,completed_quarters
from fundamentallab.technical import AlpacaMarketDataClient,YahooArgentinaMarketDataClient,TechnicalConfig,scan_technical_watchlist,DEFAULT_RSI_LIMIT,DEFAULT_MACD_PCT_LIMIT
st.set_page_config(page_title='FundamentalLab V2.12',page_icon='🚦',layout='wide')
st.title('🚦 FundamentalLab V2.12 — Multi-Market Fundamental + Technical Alerts')
st.caption(f'Prioriza automáticamente compañías que cumplen al menos {MIN_CRITERIA_TO_SURFACE} criterios.')
DB=Path('data/fundamentallab.sqlite');conn=connect(DB)
with st.sidebar:
    st.header('Configuración');email=st.text_input('Email de contacto para SEC',placeholder='tu@email.com')
    st.caption(f'SQLite: {DB}');st.caption(f'Quarters: {QUARTERS_TO_ANALYZE}');st.code('\n'.join(completed_quarters(4)))
t1,t2,t3,t4,t5=st.tabs(['🔍 Scanner','🏆 Resultados','🗄️ Base','🧩 Criterios','📡 Técnicos'])
with t1:
    a,b=st.columns(2)
    with a:
        st.subheader('🇺🇸 Estados Unidos');st.write('Universo SEC completo por compañía; datos trimestrales masivos vía XBRL Frames.')
        if st.button('Escanear USA',type='primary',use_container_width=True):
            if '@' not in email:st.error('Ingresá un email real para SEC.')
            else:
                try:
                    run=create_run(conn,['USA']);client=SecFramesClient(email)
                    with st.spinner('Descargando universo + 4Q SEC...'):
                        ctx=build_us_contexts(conn,client)
                    st.info(f'Con algún dato fundamental: {len(ctx):,}')

                    status=st.status('Evaluando compañías y precios...',expanded=True)
                    bar=st.progress(0.0)

                    def scan_progress(stage,done,total,message):
                        total=max(1,int(total or 1))
                        done=max(0,min(int(done or 0),total))
                        ratio=done/total

                        # Las fases tienen escalas distintas; la barra es orientativa.
                        if stage in ('coverage','coverage_done','evaluate','store'):
                            bar.progress(min(1.0,ratio))
                        elif stage in ('prices','prices_done'):
                            bar.progress(min(1.0,ratio))

                        status.write(message)

                    res=scan_usa(conn,run,ctx,sec_client=client,progress=scan_progress)
                    finish_run(conn,run)
                    bar.progress(1.0)
                    status.update(
                        label='Escaneo USA terminado',
                        state='complete',
                        expanded=False,
                    )
                    st.success(f'{len(res):,} evaluadas · {sum(r.surfaced for r in res):,} pasan el filtro.')
                except Exception as e:st.exception(e)
    with b:
        st.subheader('🇦🇷 Argentina');st.write('Universo BYMA líderes + panel general. Precio BYMA; fundamentals normalizados cuando están disponibles.')
        workers=st.slider('Hilos Argentina',1,10,5);limit=st.number_input('Límite de prueba (0=todas)',0,500,0,10)
        if st.button('Escanear Argentina',use_container_width=True):
            try:
                run=create_run(conn,['ARG']);client=BymaClient()

                status_arg=st.status('Escaneando Argentina...',expanded=True)
                bar_arg=st.progress(0.0)

                def arg_data_progress(done,total,message):
                    total=max(1,int(total or 1));done=max(0,min(int(done or 0),total))
                    bar_arg.progress(done/total)
                    status_arg.write(message)

                with st.spinner('Cargando universo BYMA...'):
                    ctx,u=build_argentina_contexts(
                        conn,client,workers,int(limit) or None,
                        progress=arg_data_progress
                    )

                full=sum(
                    1 for c in ctx
                    if c.metadata.get('fundamentals_status')=='ok'
                )
                partial=sum(
                    1 for c in ctx
                    if c.metadata.get('fundamentals_status')=='partial'
                )
                unavailable=sum(
                    1 for c in ctx
                    if c.metadata.get('fundamentals_status')=='unavailable'
                )

                status_arg.write(
                    f'Universo BYMA: {len(u):,} · evaluables creadas: {len(ctx):,} · '
                    f'4Q completos: {full:,} · parciales: {partial:,} · sólo N/D: {unavailable:,}'
                )

                def arg_scan_progress(stage,done,total,message):
                    total=max(1,int(total or 1))
                    done=max(0,min(int(done or 0),total))
                    bar_arg.progress(done/total)
                    status_arg.write(message)

                res=scan_argentina(
                    conn,run,ctx,
                    progress=arg_scan_progress
                )
                finish_run(conn,run)

                bar_arg.progress(1.0)
                status_arg.update(
                    label='Escaneo Argentina terminado',
                    state='complete',
                    expanded=False
                )

                st.success(
                    f'{len(res):,} especies ARG guardadas en Resultados · '
                    f'{sum(r.surfaced for r in res):,} pasan el filtro de '
                    f'{MIN_CRITERIA_TO_SURFACE}+ criterios.'
                )
                st.info(
                    'Abrí la pestaña Resultados y elegí Mercado = Argentina '
                    'para verlas exclusivamente.'
                )
            except Exception as e:st.exception(e)
with t2:
    st.subheader('Resultados por mercado')

    stats=latest_run_stats(conn)
    if not stats.empty:
        stat_cols=st.columns(len(stats))
        for col,(_,srow) in zip(stat_cols,stats.iterrows()):
            label='🇺🇸 USA' if srow['market']=='USA' else '🇦🇷 Argentina'
            col.metric(
                label,
                f"{int(srow['result_count']):,} resultados",
                f"{int(srow['surfaced_count']):,} con {MIN_CRITERIA_TO_SURFACE}+ criterios"
            )

    market_label=st.radio(
        'Mercado a mostrar',
        ['Todos','🇺🇸 USA','🇦🇷 Argentina'],
        horizontal=True,
    )
    market_code={
        'Todos':None,
        '🇺🇸 USA':'USA',
        '🇦🇷 Argentina':'ARG',
    }[market_label]

    df=latest_results(conn,market_code)

    if df.empty:
        if market_code=='ARG':
            st.warning(
                'La última corrida de Argentina no produjo filas. '
                'Volvé a Escanear Argentina con esta versión; ahora toda especie BYMA '
                'se guarda aunque sus fundamentals queden N/D.'
            )
        else:
            st.info('Todavía no hay resultados terminados para este mercado.')
    else:
        def criterion_evaluable(x):
            if 'evaluable' in x:
                return bool(x.get('evaluable'))
            # Compatibilidad con resultados viejos: inferir N/D por mensajes conocidos.
            reason=str(x.get('reason','')).lower()
            nd_tokens=('no hay 4 quarters completos','falta precio','no hay ningún share count','datos insuficientes')
            return not any(t in reason for t in nd_tokens)

        def icon(x):
            if not x or not criterion_evaluable(x):
                return '⚪ N/D'
            return '🟢' if x.get('passed') else '🔴'

        tech_latest=latest_technical_results(conn)
        tech_map={}
        if not tech_latest.empty:
            for _,tr in tech_latest.iterrows():
                tech_map[(str(tr.get('market') or 'USA'),str(tr['symbol']))]=(
                    f"{int(tr['score'])}/5" + (' 🚨' if int(tr['matched']) else '')
                )

        rows=[]
        for _,r in df.iterrows():
            criteria_list=parse_criteria_json(r['criteria_json'])
            c={x['key']:x for x in criteria_list}
            evaluated=sum(1 for x in criteria_list if criterion_evaluable(x))

            try:
                metadata=json.loads(r.get('metadata_json') or '{}')
            except Exception:
                metadata={}

            rows.append({
              'Mercado':r['market'],'Ticker':r['symbol'],'Empresa':r['name'],
              'Criterios':int(r['matches']),'Cobertura':f'{evaluated}/{len(CRITERIA)}',
              'FCF':icon(c.get('fcf_growth')),
              'Recompras':icon(c.get('buyback_growth')),
              'Margen neto':icon(c.get('net_margin_growth')),
              'Subvaluada':icon(c.get('below_intrinsic')),
              'Precio':r['price'],
              'Valor intrínseco':r['intrinsic_value'],
              'Upside':r['upside_to_intrinsic'],
              'Fuente precio':metadata.get('price_source_final') or '',
              'Último trade':(
                  metadata.get('byma_last_trade_date')
                  if r['market']=='ARG'
                  else metadata.get('price_last_date')
              ) or '',
              'Moneda fin.':metadata.get('financial_currency') or (
                  'USD' if r['market']=='USA' else ''
              ),
              'DCF status':metadata.get('dcf_quality_status') or '',
              'Técnico':tech_map.get((str(r['market']),str(r['symbol'])),'') ,
            })
        v=pd.DataFrame(rows).sort_values(['Criterios','Upside'],ascending=[False,False],na_position='last')
        top=v[v.Criterios>=MIN_CRITERIA_TO_SURFACE]
        rest=v[v.Criterios<MIN_CRITERIA_TO_SURFACE]
        st.subheader(f'🏆 Prioridad — {MIN_CRITERIA_TO_SURFACE}+ criterios')
        if market_code=='ARG' and top.empty:
            st.info(
                'Argentina fue escaneada, pero ninguna acción alcanzó todavía '
                f'{MIN_CRITERIA_TO_SURFACE}+ criterios. Las especies aparecen debajo '
                'en “Por debajo del filtro”.'
            )
        st.caption(
            '🟢 cumple · 🔴 evaluado y no cumple · ⚪ N/D = faltan datos. '
            'En Argentina el precio sólo se acepta si BYMA muestra actividad actual '
            'o un cierre reciente (máx. 30 días); cotizaciones stale se bloquean.'
        )
        st.caption('Valor intrínseco: DCF 4Q cuando hay cobertura; fallback conservador a FCF FY reciente de SEC CompanyFacts cuando 4Q no puede reconstruirse.')
        st.dataframe(top.style.format({'Precio':'{:,.2f}','Valor intrínseco':'{:,.2f}','Upside':'{:.1%}'},na_rep='N/D'),use_container_width=True,hide_index=True)
        if len(top):
            st.download_button('Descargar candidatos CSV',top.to_csv(index=False).encode('utf-8-sig'),'fundamentallab_candidates.csv','text/csv')
        st.subheader('📋 Por debajo del filtro')
        st.dataframe(rest.style.format({'Precio':'{:,.2f}','Valor intrínseco':'{:,.2f}','Upside':'{:.1%}'},na_rep='N/D'),use_container_width=True,hide_index=True)
        opts=[f"{r['Mercado']} · {r['Ticker']} · {r['Empresa']}" for _,r in v.iterrows()]
        if opts:
            s=st.selectbox('Diagnóstico',opts);m,t,_=s.split(' · ',2);raw=df[(df.market==m)&(df.symbol==t)].iloc[0]
            for x in parse_criteria_json(raw.criteria_json):
                if not criterion_evaluable(x):
                    st.info(f"⚪ **{x['label']}** — N/D — {x['reason']}")
                elif x.get('passed'):
                    st.success(f"🟢 **{x['label']}** — {x['reason']}")
                else:
                    st.error(f"🔴 **{x['label']}** — {x['reason']}")
with t3:
    st.subheader('Base local completa');st.dataframe(company_count(conn),use_container_width=True,hide_index=True)
    st.write('`companies` conserva todo el universo detectado aunque una empresa no tenga 4 quarters normalizados. `quarterly_fundamentals` y `screen_results` guardan la cobertura y resultados.')
with t4:
    st.subheader('Estrategia modular');st.write('Todo vive en `fundamentallab/criteria.py`.')
    st.dataframe(pd.DataFrame([{'Clave':c.key,'Item':c.label} for c in CRITERIA]),use_container_width=True,hide_index=True)
    c1,c2,c3,c4=st.columns(4);c1.metric('Mínimo',MIN_CRITERIA_TO_SURFACE);c2.metric('DCF años',DCF_PROJECTION_YEARS);c3.metric('Descuento',f'{DCF_DISCOUNT_RATE:.1%}');c4.metric('Terminal',f'{DCF_TERMINAL_GROWTH:.1%}')
    st.write(f'Growth cap: **{DCF_GROWTH_CAP:.1%}** · Margin of safety: **{INTRINSIC_MARGIN_OF_SAFETY:.1%}**')
    st.code(f'MIN_CRITERIA_TO_SURFACE = {MIN_CRITERIA_TO_SURFACE}\nDCF_DISCOUNT_RATE = {DCF_DISCOUNT_RATE}\nINTRINSIC_MARGIN_OF_SAFETY = {INTRINSIC_MARGIN_OF_SAFETY}',language='python')
    p=Path('fundamentallab/criteria.py')
    if p.exists():
        with st.expander('Ver criteria.py'):st.code(p.read_text(encoding='utf-8'),language='python')

with t5:
    st.subheader('📡 Scanner técnico multi-mercado + alertas')
    st.write(
        'Toma automáticamente los tickers de la última corrida fundamental del mercado elegido. '
        'No abre navegador: descarga velas por HTTP y calcula RSI/MACD% localmente.'
    )

    st.info(
        'MACD normalizado: **MACD % = (EMA12 − EMA26) / Precio × 100**. '
        'El mismo umbral puede compararse entre acciones en USD y ARS.'
    )

    market_label=st.radio(
        'Mercado técnico',
        ['🇺🇸 USA','🇦🇷 Argentina'],
        horizontal=True,
    )
    tech_market='USA' if market_label.startswith('🇺🇸') else 'ARG'

    latest_watch=latest_market_watchlist(
        conn,
        market=tech_market,
        limit=250,
        priority_only=False,
    )

    if latest_watch.empty:
        st.warning(
            f"Primero ejecutá una corrida terminada de "
            f"{'Escanear USA' if tech_market=='USA' else 'Escanear Argentina'}."
        )
    else:
        st.caption(
            f'Última corrida {tech_market}: {len(latest_watch):,} tickers disponibles para selección.'
        )

    c1,c2=st.columns(2)
    with c1:
        if tech_market=='USA':
            alpaca_key=st.text_input(
                'Alpaca API Key ID',
                type='password',
                key='alpaca_key_id',
            )
            alpaca_secret=st.text_input(
                'Alpaca API Secret Key',
                type='password',
                key='alpaca_secret_key',
            )
            feed=st.selectbox(
                'Feed Alpaca',
                ['iex','sip'],
                index=0,
                help='IEX funciona con el plan básico. SIP requiere acceso compatible.',
            )
            st.caption('USA: Alpaca batch multi-symbol para 1D / 4H / 1H.')
        else:
            alpaca_key='';alpaca_secret='';feed='iex'
            st.success('Argentina no necesita API key: usa requests HTTP a Yahoo Finance con TICKER.BA.')
            st.caption(
                'El 4H se construye localmente desde 1H. Se bloquean especies stale, '
                'con pocas velas o con volumen intradía insuficiente.'
            )

        ticker_limit=st.slider(
            'Cantidad de tickers',
            min_value=10,
            max_value=250,
            value=100,
            step=10,
            key=f'ticker_limit_{tech_market}',
        )
        priority_only=st.checkbox(
            'Sólo prioridad fundamental',
            value=False,
            key=f'priority_only_{tech_market}',
            help='Si está desactivado toma los mejores tickers de la última corrida fundamental.',
        )

    with c2:
        rsi_limit=st.number_input(
            'RSI límite',
            min_value=1.0,
            max_value=99.0,
            value=float(DEFAULT_RSI_LIMIT),
            step=1.0,
            key=f'rsi_limit_{tech_market}',
        )
        macd_pct_limit=st.number_input(
            'MACD % límite',
            min_value=-50.0,
            max_value=0.0,
            value=float(DEFAULT_MACD_PCT_LIMIT),
            step=0.1,
            format='%.2f',
            key=f'macd_limit_{tech_market}',
        )
        telegram_token=st.text_input(
            'Telegram Bot Token (opcional)',
            type='password',
            key=f'telegram_token_{tech_market}',
        )
        telegram_chat_id=st.text_input(
            'Telegram Chat ID (opcional)',
            key=f'telegram_chat_{tech_market}',
        )
        st.caption(
            'La alerta se envía sólo al ENTRAR en 5/5. '
            'El anti-spam se guarda por mercado+ticker.'
        )

    st.markdown('**Condición de alerta 5/5**')
    st.code(
        f"1D  RSI < {rsi_limit:g}\n"
        f"1D  MACD% < {macd_pct_limit:g}%\n"
        f"4H  RSI < {rsi_limit:g}\n"
        f"4H  MACD% < {macd_pct_limit:g}%\n"
        f"1H  RSI < {rsi_limit:g}"
    )

    button_label=(
        'Escanear técnicos USA ahora'
        if tech_market=='USA'
        else 'Escanear técnicos Argentina ahora'
    )

    if st.button(button_label,type='primary',use_container_width=True,key=f'run_tech_{tech_market}'):
        if tech_market=='USA' and (not alpaca_key or not alpaca_secret):
            st.error('Ingresá Alpaca API Key ID y Secret Key.')
        else:
            watch=latest_market_watchlist(
                conn,
                market=tech_market,
                limit=int(ticker_limit),
                priority_only=bool(priority_only),
            )

            if watch.empty:
                st.warning(f'No hay tickers {tech_market} para monitorear con ese filtro.')
            else:
                status_tech=st.status(f'Escaneando técnicos {tech_market}...',expanded=True)
                bar_tech=st.progress(0.0)

                def tech_progress(stage,done,total,message):
                    total=max(1,int(total or 1))
                    done=max(0,min(int(done or 0),total))
                    bar_tech.progress(done/total)
                    status_tech.write(message)

                try:
                    if tech_market=='USA':
                        client=AlpacaMarketDataClient(
                            alpaca_key,
                            alpaca_secret,
                            feed=feed,
                        )
                    else:
                        client=YahooArgentinaMarketDataClient(
                            max_workers=10,
                        )

                    config=TechnicalConfig(
                        rsi_limit=float(rsi_limit),
                        macd_pct_limit=float(macd_pct_limit),
                    )
                    results=scan_technical_watchlist(
                        conn,
                        watch['symbol'].astype(str).tolist(),
                        client,
                        config=config,
                        telegram_bot_token=telegram_token or None,
                        telegram_chat_id=telegram_chat_id or None,
                        progress=tech_progress,
                    )
                    bar_tech.progress(1.0)
                    status_tech.update(
                        label=f'Scanner técnico {tech_market} terminado',
                        state='complete',
                        expanded=False,
                    )
                    matched=sum(bool(x['matched']) for x in results)
                    new_alerts=sum(bool(x['alert_triggered']) for x in results)
                    evaluable=sum(bool(x['evaluable']) for x in results)
                    st.success(
                        f'{len(results):,} tickers evaluados · '
                        f'{evaluable:,} con cobertura 5/5 · '
                        f'{matched:,} actualmente 5/5 · '
                        f'{new_alerts:,} alertas nuevas.'
                    )
                except Exception as e:
                    status_tech.update(
                        label='Error en scanner técnico',
                        state='error',
                        expanded=True,
                    )
                    st.exception(e)

    tech_df=latest_technical_results(conn,market=tech_market)
    tech_run=latest_technical_run(conn,market=tech_market)

    if not tech_run.empty:
        rr=tech_run.iloc[0]
        st.caption(
            f"Último scan técnico {tech_market}: {rr['finished_at']} · "
            f"fuente: {rr['source']} · RSI<{rr['rsi_limit']:g} · "
            f"MACD%<{rr['macd_pct_limit']:g}%"
        )

    if not tech_df.empty:
        def cond_icon(value):
            if value is None:
                return '⚪'
            return '✅' if bool(value) else '❌'

        rows=[]
        for _,r in tech_df.iterrows():
            try:
                cc=json.loads(r['conditions_json'] or '{}')
            except Exception:
                cc={}
            try:
                qq=json.loads(r.get('quality_json') or '{}')
            except Exception:
                qq={}

            quality_parts=[]
            for tf in ('1D','4H','1H'):
                status=(qq.get(tf) or {}).get('status')
                if status and status!='ok':
                    quality_parts.append(f'{tf}:{status}')

            rows.append({
                'Mercado':r.get('market') or tech_market,
                'Ticker':r['symbol'],
                'Score':f"{int(r['score'])}/5" + (' 🚨' if int(r['matched']) else ''),
                '1D RSI':r['rsi_1d'],
                '1D RSI OK':cond_icon(cc.get('day_rsi')),
                '1D MACD%':r['macd_pct_1d'],
                '1D MACD OK':cond_icon(cc.get('day_macd_pct')),
                '4H RSI':r['rsi_4h'],
                '4H RSI OK':cond_icon(cc.get('h4_rsi')),
                '4H MACD%':r['macd_pct_4h'],
                '4H MACD OK':cond_icon(cc.get('h4_macd_pct')),
                '1H RSI':r['rsi_1h'],
                '1H RSI OK':cond_icon(cc.get('h1_rsi')),
                'Precio':r['price'],
                'Nueva alerta':'🚨' if int(r['alert_triggered']) else '',
                'Calidad':' · '.join(quality_parts) if quality_parts else 'OK',
                'Última vela 1H':r['bar_1h'],
            })

        tv=pd.DataFrame(rows)
        st.subheader(f'Estado técnico actual — {tech_market}')
        st.dataframe(
            tv.style.format({
                '1D RSI':'{:.2f}',
                '1D MACD%':'{:.2f}%',
                '4H RSI':'{:.2f}',
                '4H MACD%':'{:.2f}%',
                '1H RSI':'{:.2f}',
                'Precio':'{:,.2f}',
            },na_rep='N/D'),
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            'Descargar técnicos CSV',
            tv.to_csv(index=False).encode('utf-8-sig'),
            f'fundamentallab_technical_{tech_market.lower()}.csv',
            'text/csv',
            key=f'download_tech_{tech_market}',
        )

        near=tv[tv['Score'].str.startswith('4/5')]
        if len(near):
            st.subheader('🟠 Cerca de disparar — 4/5')
            st.dataframe(near,use_container_width=True,hide_index=True)

        if tech_market=='ARG':
            bad=tv[tv['Calidad']!='OK']
            if len(bad):
                with st.expander(f'⚪ Argentina — {len(bad)} tickers con cobertura/calidad insuficiente'):
                    st.dataframe(
                        bad[['Ticker','Calidad','Última vela 1H']],
                        use_container_width=True,
                        hide_index=True,
                    )
