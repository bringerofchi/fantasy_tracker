from espn_adapter import ESPNSourceAdapter

adapter = ESPNSourceAdapter()
obs = adapter.fetch(season_id=2025, week_number=5, player_ids=[4429795])
for o in obs:
    print(o.data_type, o.ranking_type, o.fantasy_points, o.rank)