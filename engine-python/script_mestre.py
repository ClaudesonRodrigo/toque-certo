import requests
import os
import math
from dotenv import load_dotenv
import time
from supabase import create_client, Client

# ==========================================
# 1. SETUP INICIAL E CONEXÕES
# ==========================================
load_dotenv()
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY"))

API_TOKEN = os.getenv("API_TOKEN") or os.getenv("FOOTBALL_DATA_API_KEY") or os.getenv("API_FOOTBALL_KEY")
BASE_URL = "https://api.football-data.org/v4"
headers = {"X-Auth-Token": API_TOKEN}

# ==========================================
# 2. FUNÇÕES DO MOTOR (OTIMIZADAS)
# ==========================================
def buscar_jogos_do_dia(competition_code, season):
    """Busca a temporada inteira na football-data.org e separa 5 jogos na memória."""
    url = f"{BASE_URL}/competitions/{competition_code}/matches"
    params = {"season": season}
    print(f"🔍 [1/3] Baixando temporada {season} da competição {competition_code}...")

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if response.status_code == 403:
            print("\n🚨 ERRO 403: recurso restrito para seu plano na football-data.org.")
            return []
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as e:
        print(f"\n🚨 ERRO NA API (BUSCAR JOGOS): {e}")
        return []

    todos_os_jogos = payload.get("matches", [])
    if len(todos_os_jogos) > 5:
        jogos_simulados = todos_os_jogos[100:105] if len(todos_os_jogos) > 105 else todos_os_jogos[:5]
        print(f"✅ Download concluído! {len(jogos_simulados)} confrontos separados para análise.")
        return jogos_simulados

    return todos_os_jogos


def extrair_historico_lote(teams_ids, season):
    """Baixa o histórico dos times com anti-bloqueio e timeout."""
    print(f"📦 [2/3] Baixando histórico de {len(teams_ids)} times...")

    teams_batch = []
    matches_batch = []
    stats_batch = []

    total_times = len(teams_ids)

    for index, team_id in enumerate(teams_ids, 1):
        print(f"   ⏳ [{index}/{total_times}] Extraindo dados do Time ID {team_id}...")
        url = f"{BASE_URL}/teams/{team_id}/matches"
        params = {"season": season, "status": "FINISHED", "limit": 10}

        try:
            response = requests.get(url, headers=headers, params=params, timeout=15)
            if response.status_code == 403:
                print(f"   🚨 Acesso restrito ao histórico do Time {team_id}. Ignorando...")
                continue

            response.raise_for_status()
            payload = response.json()
            jogos = payload.get("matches", [])

            for j in jogos[-10:]:
                m_id = j["id"]
                home = j["homeTeam"]
                away = j["awayTeam"]
                score_full = j.get("score", {}).get("fullTime", {})

                teams_batch.append({"id": home["id"], "name": home["name"]})
                teams_batch.append({"id": away["id"], "name": away["name"]})
                matches_batch.append(
                    {
                        "id": m_id,
                        "team_home": home["id"],
                        "team_away": away["id"],
                        "date": j["utcDate"],
                    }
                )

                if score_full.get("home") is not None and score_full.get("away") is not None:
                    stats_batch.append(
                        {
                            "match_id": m_id,
                            "goals_home": score_full["home"],
                            "goals_away": score_full["away"],
                        }
                    )

        except requests.exceptions.RequestException:
            print(f"   ❌ Falha de conexão ao baixar Time {team_id}. Ignorando...")

        time.sleep(1.0)

    teams_batch = list({t["id"]: t for t in teams_batch}.values())
    matches_batch = list({m["id"]: m for m in matches_batch}.values())
    stats_batch = list({s["match_id"]: s for s in stats_batch}.values())

    print(f"⚡ Enviando lote ao Supabase: {len(teams_batch)} times, {len(matches_batch)} jogos...")
    if teams_batch:
        supabase.table("teams").upsert(teams_batch).execute()
    if matches_batch:
        supabase.table("matches").upsert(matches_batch).execute()
    if stats_batch:
        supabase.table("stats").upsert(stats_batch).execute()


def calcular_poisson_lote(jogos_hoje):
    """Baixa o banco uma vez e calcula probabilidades em lote."""
    print("🧠 [3/3] Calculando Poisson para todos os jogos...")

    stats = supabase.table("stats").select("*").execute().data or []
    matches = supabase.table("matches").select("*").execute().data or []

    analysis_batch = []

    for jogo in jogos_hoje:
        match_id = jogo["id"]
        home_id = jogo["homeTeam"]["id"]
        away_id = jogo["awayTeam"]["id"]

        def obter_media_ataque(team_id):
            gols = 0
            contagem = 0
            for m in matches:
                s = next((x for x in stats if x["match_id"] == m["id"]), None)
                if s:
                    if m["team_home"] == team_id:
                        gols += s["goals_home"]
                        contagem += 1
                    elif m["team_away"] == team_id:
                        gols += s["goals_away"]
                        contagem += 1
            return gols / contagem if contagem > 0 else 0.1

        lambda_home = obter_media_ataque(home_id)
        lambda_away = obter_media_ataque(away_id)
        lambda_jogo = lambda_home + lambda_away

        def poisson(lmbd, x):
            return (math.exp(-lmbd) * (lmbd**x)) / math.factorial(x)

        prob_0_gols = poisson(lambda_jogo, 0)
        prob_1_gol = poisson(lambda_jogo, 1)
        prob_2_gols = poisson(lambda_jogo, 2)

        prob_over_0_5 = (1 - prob_0_gols) * 100
        prob_over_1_5 = (1 - (prob_0_gols + prob_1_gol)) * 100
        prob_over_2_5 = (1 - (prob_0_gols + prob_1_gol + prob_2_gols)) * 100
        prob_ambas_marcam = ((1 - poisson(lambda_home, 0)) * (1 - poisson(lambda_away, 0))) * 100

        analysis_batch.append(
            {
                "match_id": match_id,
                "prob_goals": round(prob_over_0_5, 2),
                "prob_over_1_5": round(prob_over_1_5, 2),
                "prob_over_2_5": round(prob_over_2_5, 2),
                "prob_btts": round(prob_ambas_marcam, 2),
            }
        )

    if analysis_batch:
        supabase.table("analysis").upsert(analysis_batch).execute()


# ==========================================
# 3. EXECUÇÃO PRINCIPAL
# ==========================================
print("🤖 Iniciando Motor V2 (football-data.org)...")
COMPETITION_CODE = os.getenv("COMPETITION_CODE", "BSB")
SEASON = int(os.getenv("SEASON", "2024"))

jogos_hoje = buscar_jogos_do_dia(COMPETITION_CODE, SEASON)

if not jogos_hoje:
    print("📅 Nenhum jogo encontrado ou erro na API.")
else:
    times_ids = set()
    matches_do_dia = []

    for jogo in jogos_hoje:
        times_ids.add(jogo["homeTeam"]["id"])
        times_ids.add(jogo["awayTeam"]["id"])
        matches_do_dia.append(
            {
                "id": jogo["id"],
                "team_home": jogo["homeTeam"]["id"],
                "team_away": jogo["awayTeam"]["id"],
                "date": jogo["utcDate"],
            }
        )

    extrair_historico_lote(times_ids, SEASON)

    print(f"📌 Garantindo {len(matches_do_dia)} confrontos base no Supabase...")
    supabase.table("matches").upsert(matches_do_dia).execute()

    calcular_poisson_lote(jogos_hoje)

    print("\n🎉 Sucesso! Migração para football-data.org concluída. ⚡")
