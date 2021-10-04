from itertools import product
from typing import Tuple, Dict

import joblib
import numpy as np
import pandas as pd

from glicko2 import Glicko2, Rating
from utils.metrics import three_outcomes_log_loss


class GlickoSoccer(object):

    def __init__(self, is_draw_mode=True, init_mu=1500, init_rd=120, update_rd=30, lift_update_mu=0,
                 home_advantage=30, draw_inclination=-0.22, cup_penalty=10, new_team_update_mu=-20):
        self.is_draw_mode = is_draw_mode
        self.init_mu = init_mu
        self.init_rd = init_rd
        self.lift_update_mu = lift_update_mu
        self.update_rd = update_rd
        self.home_advantage = home_advantage
        self.draw_inclination = draw_inclination
        self.cup_penalty = cup_penalty
        self.new_team_update_mu = new_team_update_mu
        self.euro_cups = {'Champions League', 'Europa League'}  # !!!!

    def preprocessing(self, results: pd.DataFrame) -> pd.DataFrame:
        """"""
        rename_teams = {

        }

        results['home_team'] = results['home_team'].map(lambda x: x.replace("'", "").strip())
        results['away_team'] = results['away_team'].map(lambda x: x.replace("'", "").strip())

        results['home_team'] = results['home_team'].map(lambda x: rename_teams[x] if x in rename_teams else x)
        results['away_team'] = results['away_team'].map(lambda x: rename_teams[x] if x in rename_teams else x)

        results = results.loc[results['home_score'] != '-']

        results[['home_score', 'away_score']] = results[['home_score', 'away_score']].to_numpy('int')

        conditions = [(results['home_score'] > results['away_score']),
                      (results['home_score'] == results['away_score']),
                      (results['home_score'] < results['away_score'])]

        outcomes = ['H', 'D', 'A']
        results['outcome'] = np.select(conditions, outcomes)

        results = self._remove_matches_with_unknown_team(results)

        results = self._average_scoring(results)

        results = results.drop(columns=['home_score', 'away_score']).sort_values(['date'])

        return results

    def _average_scoring(self, results: pd.DataFrame) -> pd.DataFrame:
        """"""

        results['total'] = (results['home_score'] + results['away_score'])

        goals = pd.concat([results.loc[:, ['index', 'home_team', 'total', 'date']]
                          .rename(columns={'home_team': 'team'}),

                           results.loc[:, ['index', 'away_team', 'total', 'date']]
                          .rename(columns={'away_team': 'team'})])

        mean = (goals
                .sort_values(['team', 'date'], ascending=[True, True])
                .groupby(['team'], sort=False)
                ['total']
                .shift()
                .rolling(10, min_periods=3)
                .mean()
                .reset_index())

        mean['total'] = mean['total'].fillna(2.5)

        mean = (mean
                .groupby(['index'])
                ['total']
                .mean()
                .to_dict())

        results = results.reset_index()

        results['avg_scoring'] = results['level_0'].map(mean)

        results = results.drop(columns=['total', 'level_0'])

        return results

    @staticmethod
    def _team_leagues(results: pd.DataFrame, season: int) -> dict:
        """"""
        no_cups = (results.loc[(results['tournament_type'] != 3)
                               & (results['season'] == season), ['home_team', 'away_team', 'tournament']])

        team_leagues = dict(zip(no_cups['home_team'], no_cups['tournament']))

        team_leagues.update(dict(zip(no_cups['away_team'], no_cups['tournament'])))

        return team_leagues

    def _remove_matches_with_unknown_team(self, results: pd.DataFrame) -> pd.DataFrame:
        """Remove matches between teams from leagues we dont know anything about."""
        for season in results['season'].unique():
            team_leagues = self._team_leagues(results, season)

            known_teams = team_leagues.keys()

            is_both_teams_known = ((results['season'] == season)
                                   & results['home_team'].isin(known_teams)
                                   & results['away_team'].isin(known_teams))

            results = results.loc[(results['season'] != season) | is_both_teams_known]

        return results

    def _team_international_cups(self, results: pd.DataFrame, season: int) -> set:
        """
        """

        international_cups = results.loc[results['tournament'].isin(self.euro_cups) & (results['season'] == season)]

        international_teams = set(international_cups['home_team']).union(set(international_cups['away_team']))

        return international_teams

    def _league_params_initialization(self, results: pd.DataFrame) -> dict:
        """"""
        leagues = results.loc[results['tournament_type'] != 3, 'tournament'].unique()
        league_params = dict()
        for league in leagues:
            league_params[league] = {'init_mu': self.init_mu,
                                     'init_rd': self.init_rd,
                                     'update_rd': self.update_rd,
                                     'lift_update_mu': self.lift_update_mu,
                                     'home_advantage': self.home_advantage,
                                     'cup_penalty': self.cup_penalty,
                                     'new_team_update_mu': self.new_team_update_mu}

        return league_params

    @staticmethod
    def _team_params(season: int, league_params: dict, team_leagues_all: dict) -> dict:
        """
            For each team get league params.
        """

        season_team_leagues = team_leagues_all[season]

        team_params = {team: league_params[league] for team, league in season_team_leagues.items()}

        return team_params

    @staticmethod
    def _rating_initialization(results: pd.DataFrame, team_params: dict) -> dict:
        """"""
        seasons = sorted(results['season'].unique())

        ratings = dict()
        for season in seasons:
            for team, params in team_params[season].items():
                if team not in ratings:
                    ratings[team] = Rating(mu=params['init_mu'], rd=params['init_rd'])

        return ratings

    @staticmethod
    def _update_ratings_indexes(results: pd.DataFrame) -> Tuple[dict, dict, dict]:
        """"""
        no_cups = results.loc[results['tournament_type'] != 3]

        no_cups = pd.concat([no_cups.loc[:, ['date', 'index', 'home_team', 'season', 'tournament']]
                            .rename(columns={'home_team': 'team'}),
                             no_cups.loc[:, ['date', 'index', 'away_team', 'season', 'tournament']]
                            .rename(columns={'away_team': 'team'})])

        no_cups = (no_cups
                   .sort_values(['team', 'date'])
                   .drop_duplicates(['team', 'season'], keep='first'))

        # teams missed previous season
        # remove first seasons too
        missed_previous_season = (no_cups
                                  .loc[(no_cups['season'] != no_cups['season'].shift() + 1)
                                       & (no_cups['team'] == no_cups['team'].shift())]
                                  .groupby(['team'])
                                  ['index']
                                  .apply(set)
                                  .to_dict())

        # teams changed league
        changed_league = (no_cups
                          .loc[(no_cups['season'] == no_cups['season'].shift() + 1)
                               & (no_cups['tournament'] != no_cups['tournament'].shift())
                               & (no_cups['team'] == no_cups['team'].shift())]
                          .groupby(['team'])
                          ['index']
                          .apply(set)
                          .to_dict())

        # the same league
        same_league = (no_cups
                       .loc[(no_cups['season'] == no_cups['season'].shift() + 1)
                            & (no_cups['tournament'] == no_cups['tournament'].shift())
                            & (no_cups['team'] == no_cups['team'].shift())]
                       .groupby(['team'])
                       ['index']
                       .apply(set)
                       .to_dict())

        return missed_previous_season, changed_league, same_league

    @staticmethod
    def _season_update_rating(ratings: dict, home_team: str, away_team: str, index: int,
                              home_params: dict, away_params: dict,
                              missed_previous_season: Dict[str, set], changed_league: Dict[str, set],
                              same_league: Dict[str, set]) -> dict:
        """"""
        for team in [home_team, away_team]:
            if team == home_team:
                params = home_params
            else:
                params = away_params

            if team in missed_previous_season:
                if index in missed_previous_season[team]:
                    ratings[team] = Rating(mu=params['init_mu'] + params['new_team_update_mu'], rd=params['init_rd'])

            elif team in changed_league:
                if index in changed_league[team]:
                    ratings[team] = Rating(mu=ratings[team].mu + params['lift_update_mu'],
                                           rd=ratings[team].rd + params['update_rd'])

            elif team in same_league:
                if index in same_league[team]:
                    ratings[team] = Rating(mu=ratings[team].mu, rd=ratings[team].rd + params['update_rd'])

        return ratings

    def _indexes_for_update(self, results: pd.DataFrame) -> Tuple[dict, dict, dict, set, set]:
        """"""

        team_leagues = {season: self._team_leagues(results, season) for season in set(results['season'])}

        eurocups_teams = {season: self._team_international_cups(results, season) for season in set(results['season'])}

        missed_previous_season, changed_league, same_league = self._update_ratings_indexes(results)

        indexes_for_update_ratings = set()
        eurocup_in_cups_indexes = set()
        for row in results.itertuples():
            index, home_team, away_team, season = row.index, row.home_team, row.away_team, row.season

            for team in [home_team, away_team]:
                if team in missed_previous_season:
                    if index in missed_previous_season[team]:
                        indexes_for_update_ratings.add(index)

                elif team in changed_league:
                    if index in changed_league[team]:
                        indexes_for_update_ratings.add(index)

                elif team in same_league:
                    if index in same_league[team]:
                        indexes_for_update_ratings.add(index)

            euro = eurocups_teams[season]
            if ((home_team in euro) and (away_team not in euro)) or ((home_team not in euro) & (away_team in euro)):
                if ((row.tournament_type == 3)
                        and (row.tournament not in self.euro_cups)
                        and (team_leagues[season][home_team] != team_leagues[season][away_team])):
                    eurocup_in_cups_indexes.add(index)

        return missed_previous_season, changed_league, same_league, indexes_for_update_ratings, eurocup_in_cups_indexes

    def rate_teams(self, results: pd.DataFrame, league_params: dict) -> dict:
        """"""

        glicko = Glicko2(draw_inclination=self.draw_inclination)

        seasons = set(results['season'])

        eurocups_teams = {season: self._team_international_cups(results, season) for season in seasons}
        team_leagues_all = {season: self._team_leagues(results, season) for season in seasons}

        missed_prev, changed, same, indexes_for_update, eurocup_in_cups = self._indexes_for_update(results)
        results = results.drop(columns=['date', 'country'])

        team_params = {season: self._team_params(season, league_params, team_leagues_all) for season
                       in results['season'].unique()}

        ratings = self._rating_initialization(results, team_params)

        for row in results.itertuples(index=False):

            index, home_team, away_team, season = row.index, row.home_team, row.away_team, row.season
            outcome, avg_scoring = row.outcome, row.avg_scoring

            home_params = team_params[season][home_team]
            away_params = team_params[season][away_team]

            if index in indexes_for_update:
                ratings = self._season_update_rating(ratings, home_team, away_team, index, home_params, away_params,
                                                     missed_prev, changed, same)

            home_advantage = home_params['home_advantage']

            if index in eurocup_in_cups:
                if home_team in eurocups_teams[season]:
                    home_advantage -= home_params['cup_penalty']
                else:
                    home_advantage += away_params['cup_penalty']

            # get current team ratings
            home_rating, away_rating = ratings[home_team], ratings[away_team]

            # update team ratings
            ratings[home_team], ratings[away_team] = glicko.rate(home_rating, away_rating, home_advantage,
                                                                 outcome,
                                                                 avg_scoring)

        return ratings

    def calculate_loss(self, results: pd.DataFrame, league_params: dict, team_leagues_all: dict,
                       missed_previous_season: dict, changed_league: dict, same_league: dict,
                       indexes_for_update_ratings: set, eurocup_in_cups_indexes: set,
                       eurocups_teams: dict, draw_inclination: float) -> float:
        """"""

        glicko = Glicko2(draw_inclination=draw_inclination)

        team_params = {season: self._team_params(season, league_params, team_leagues_all) for season
                       in results['season'].unique()}

        ratings = self._rating_initialization(results, team_params)

        log_loss_value = 0
        for row in results.itertuples(index=False):

            index, home_team, away_team, season = row.index, row.home_team, row.away_team, row.season
            outcome, avg_scoring = row.outcome, row.avg_scoring

            home_params = team_params[season][home_team]
            away_params = team_params[season][away_team]

            if index in indexes_for_update_ratings:
                ratings = self._season_update_rating(ratings, home_team, away_team, index, home_params, away_params,
                                                     missed_previous_season, changed_league, same_league)

            home_advantage = home_params['home_advantage']

            if index in eurocup_in_cups_indexes:
                if home_team in eurocups_teams[season]:
                    home_advantage -= home_params['cup_penalty']
                else:
                    home_advantage += away_params['cup_penalty']

            # get current team ratings
            home_rating, away_rating = ratings[home_team], ratings[away_team]

            # calculate outcome probabilities
            win_probability, tie_probability, loss_probability = glicko.probabilities(home_rating, away_rating,
                                                                                      home_advantage, avg_scoring)

            log_loss_value += three_outcomes_log_loss(outcome, win_probability, tie_probability, loss_probability)

            # update team ratings
            ratings[home_team], ratings[away_team] = glicko.rate(home_rating, away_rating, home_advantage,
                                                                 outcome,
                                                                 avg_scoring)

        return log_loss_value

    def fit_params(self, results: pd.DataFrame, number_iterations: int, is_params_initialization: True):
        """"""

        eurocups_teams = dict()
        for season in results['season'].unique():
            eurocups_teams[season] = self._team_international_cups(results, season)

        if is_params_initialization:
            league_params = self._league_params_initialization(results)
        else:
            league_params = joblib.load('data/league_params.pkl')

        first_leagues = set(results.loc[results['tournament_type'] == 1, 'tournament'])

        seasons = set(results['season'])

        eurocups_teams = {season: self._team_international_cups(results, season) for season in seasons}
        team_leagues_all = {season: self._team_leagues(results, season) for season in seasons}

        missed_prev, changed, same, indexes_for_update, eurocup_in_cups = self._indexes_for_update(results)
        results = results.drop(columns=['date', 'country'])

        draw_inclination = self.draw_inclination

        current_loss = self.calculate_loss(results, league_params, team_leagues_all, missed_prev, changed, same,
                                           indexes_for_update, eurocup_in_cups, eurocups_teams, draw_inclination)

        print("Current Loss:", current_loss)

        for i in range(number_iterations):
            draw_inclination_list = np.linspace(draw_inclination - 0.02, draw_inclination + 0.02, 11)

            for draw in draw_inclination_list:

                loss = self.calculate_loss(results, league_params, team_leagues_all, missed_prev, changed, same,
                                           indexes_for_update, eurocup_in_cups, eurocups_teams, draw)

                if loss < current_loss:
                    current_loss = loss
                    draw_inclination = draw

            print("Best Draw Parameter:", draw_inclination)

            for league, params in league_params.items():

                init_mu = params['init_mu']
                init_rd = params['init_rd']
                update_rd = params['update_rd']
                lift_update_mu = params['lift_update_mu']
                home_advantage = params['home_advantage']
                cup_penalty = params['cup_penalty']
                new_team_update_mu = params['new_team_update_mu']

                init_mu_list = [init_mu - 20, init_mu, init_mu + 20]
                init_rd_list = [init_rd]
                update_rd_list = [update_rd - 10, update_rd, update_rd + 10]
                lift_update_mu_list = [lift_update_mu]
                home_advantage_list = [home_advantage - 3, home_advantage, home_advantage + 3]
                cup_penalty_list = [cup_penalty]
                new_team_update_mu_list = [new_team_update_mu]

                init_rd_list = [x for x in init_rd_list if x >= 100]
                update_rd_list = [x for x in update_rd_list if x >= 20]

                if league in first_leagues:
                    new_team_update_mu_list = [0]
                else:
                    cup_penalty_list = [0]

                params_list = list(product(init_mu_list,
                                           init_rd_list,
                                           update_rd_list,
                                           lift_update_mu_list,
                                           home_advantage_list,
                                           cup_penalty_list,
                                           new_team_update_mu_list))

                params_loss = {params: 0 for params in params_list}
                for params in params_list:
                    league_params[league] = {'init_mu': params[0],
                                             'init_rd': params[1],
                                             'update_rd': params[2],
                                             'lift_update_mu': params[3],
                                             'home_advantage': params[4],
                                             'cup_penalty': params[5],
                                             'new_team_update_mu': params[6]}

                    params_loss[params] = self.calculate_loss(results, league_params, team_leagues_all,
                                                              missed_prev, changed, same, indexes_for_update,
                                                              eurocup_in_cups, eurocups_teams, draw_inclination)

                optimal_params = min(params_loss, key=params_loss.get)

                optimal_params_dict = {'init_mu': optimal_params[0],
                                       'init_rd': optimal_params[1],
                                       'update_rd': optimal_params[2],
                                       'lift_update_mu': optimal_params[3],
                                       'home_advantage': optimal_params[4],
                                       'cup_penalty': optimal_params[5],
                                       'new_team_update_mu': optimal_params[6]}

                league_params[league] = optimal_params_dict

                print(league, int(params_loss[optimal_params]))
                print(optimal_params_dict)

                joblib.dump(league_params, 'data/league_params.pkl')

        return league_params

    def ratings_to_df(self, ratings: dict, results: pd.DataFrame) -> pd.DataFrame:
        """"""
        seasons = sorted(results['season'].unique())

        ratings = {team: rating.mu for team, rating in ratings.items()}

        max_seasons = (results
                       .sort_values(['tournament', 'season'], ascending=False)
                       .drop_duplicates(['tournament'], keep='first'))

        max_seasons = dict(zip(max_seasons['tournament'], max_seasons['season']))

        results['max_season'] = results['tournament'].map(max_seasons)

        results = results.loc[results['season'] == results['max_season']]

        team_leagues = self._team_leagues(results, min(seasons))
        for season in seasons:
            team_leagues.update(self._team_leagues(results, season))

        ratings_df = (pd.DataFrame
                      .from_dict(ratings, orient='index')
                      .reset_index()
                      .rename(columns={'index': 'team', 0: 'rating'})
                      .sort_values(['rating'], ascending=False)
                      .reset_index(drop=True)
                      .reset_index()
                      .rename(columns={'index': '#'}))

        ratings_df['#'] = (ratings_df['#'] + 1)

        ratings_df['league'] = ratings_df['team'].map(team_leagues)

        return ratings_df

    @staticmethod
    def league_ratings(ratings: pd.DataFrame, number_top_teams=50) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """"""
        leagues_ratings = (ratings
                           .groupby(['league'])
                           .apply(lambda x: x.nlargest(number_top_teams, 'rating'))
                           .reset_index(drop=True))

        leagues_ratings = (leagues_ratings
                           .groupby(['league'])
                           ['rating']
                           .mean()
                           .reset_index()
                           .sort_values(['rating'], ascending=False)
                           .reset_index(drop=True)
                           .reset_index()
                           .rename(columns={'index': '#'}))

        leagues_ratings['#'] = (leagues_ratings['#'] + 1)

        return leagues_ratings
