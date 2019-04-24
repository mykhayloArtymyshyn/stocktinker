#!/bin/env python
import sys
import os
import csv
import datetime
import time

from slugify import slugify
import pandas as pd
import numpy as np

import openpyxl as xls
from openpyxl.utils.dataframe import dataframe_to_rows

if sys.version_info[0] >= 3:
    from urllib.request import urlretrieve
    import urllib.request
else:
    # Not Python 3 - today, it is most likely to be Python 2
    # But note that this might need an update when Python 4
    # might be around one day
    from urllib import urlretrieve

import locale

from locale import atof
locale.setlocale(locale.LC_NUMERIC, '')


# try to fetch the path to store financials data
morningstar_data_path = os.getenv("STOCKTINKER_DATA", "morningstar_data")
if not os.path.exists(morningstar_data_path):
    os.makedirs(morningstar_data_path)

# try to fetch the path to store reports
reports_path = os.getenv("STOCKTINKER_REPORTS", "reports")
if not os.path.exists(reports_path):
    os.makedirs(reports_path)

class MorningStarPriceError(Exception):
    pass

class Stock():
    ''' Class to receive and process financial data from for stocks using morningstar

        This class loads finacial data and key ratios from morningstar and loads
        them into pandas dataframes. The daa is cached locally in csv files.
        Three types of reports are available:
            "income", "cashflow", "balancesheet", "ratios"

    '''
    # map between morningstar finacial report keys and report types
    report_key_map = {'income' : 'is',
                      'cashflow' : 'cf',
                      'balancesheet' : 'bs'}

    def __init__(self,
                 symbol,
                 force_update=False,
                 filters=None,
                 stock_index_key=None,
                 revenue_growth_log_shift=2,
                 n_projection_years=10,
                 target_yield=0.15):
        self.symbol = symbol
        self.force_update = force_update
        self.stock_index_key = stock_index_key
        self.revenue_growth_log_shift = revenue_growth_log_shift
        self.n_projection_years = n_projection_years
        self.target_yield = target_yield
        self._company_name = None
        self._income = None
        self._cashflow = None
        self._balancesheet = None
        self._ratios = None
        self._quote = None
        self._historic_prices = None
        self._filters = filters
        self._target_pe = None
        self._estimated_growth = None
        self._expected_dividends = None
        self._projected_dividends_growth = None
        self.error_log = []
        if self._filters is None:
            self._filters = []

    def passes_filters(self):
        for f in self._filters:
            print ("checking "+  f.name)
            if not f.check(self):
                return False
        return True

    def write_xls_report(self):
        ''' create a excel report for a single stock '''
        wb = xls.Workbook(write_only=True)
        summary_ws = self._df_to_ws(self.get_summary_df().transpose(), wb, 'summary')
        self._df_to_ws(self.income.transpose(), wb, 'income')
        self._df_to_ws(self.cashflow.transpose(), wb, 'cashflow')
        self._df_to_ws(self.balancesheet.transpose(), wb, 'balancesheet')
        self._df_to_ws(self.ratios.transpose(), wb, 'ratios')
        for key, val in self.get_summary_info():
            summary_ws.append([key, val])

        filename= "report-{symbol}.xlsx".format(symbol=self.symbol)
        wb.save(os.path.join(reports_path,filename))

    def get_summary_info(self):
        try:
            summary_infos = [
                ("Growth rates","%"),
                ("average growth", 100 * self.estimated_growth),
                ("estimated eps growth", 100 * self.estimated_eps_growth),
                ("estimated revenue growth", 100 * self.estimated_revenue_growth),
                ("estimated bookvalue growth", 100 * self.estimated_bookvalue_growth),
                ("estimated operational cashflow growth", 100 * self.estimated_operational_cashflow_growth),
                ("",""),
                ("currency", self.currency),
                ("eps TTM", self.ratios["earnings-per-share-%s" % self.currency].iloc[-1]),

            ]

            if 'pe' in self.ratios:
                summary_infos += [("min p/e", min(self.ratios["pe"]) ),
                                    ("max p/e", max(self.ratios["pe"])),
                                    ("0.75 perc p/e", self.ratios["pe"].quantile(q=0.75, interpolation='linear')),
                                    ("",""),]
        except KeyError as e:
            self.error_log.append("Field not found {}".format(str(e)))
            return []
        def format(value):
            try:
                return  "%.2f" % value
            except:
                return value
        return [(s[0], format(s[1])) for s in summary_infos ]


    @staticmethod
    def get_summary_header_row():
        return [
                'company name',
                'symbol',
                'current price',
                'target_price',
                'eps',
                'max p/e',
                'est. growth rate',
                'target p/e',
                'target eps',
                'price projection'
                ]

    def get_summary_row(self):
        return [ self.company_name,
                 self.symbol,
                 self.current_price,
                 self.target_price,
                 self.ratios["earnings-per-share-%s" % self.currency].iloc[-1],
                 max(self.ratios["pe"]),
                 self.estimated_growth,
                 self.target_pe,
                 self.get_estimated_eps(self.n_projection_years),
                 self.price_projection,
                ]

    @property
    def currency(self):
        ''' Dirty hack to get currency '''
        for key in list(self.ratios):
            if key.startswith('earnings-per-share-'):
                return key.split('earnings-per-share-')[1]

    @property
    def company_name(self):
        if self._company_name is None:
            with open(self.report_path("ratios"),"r") as csvfile:
                reader = csv.reader(csvfile)
                for i,row in enumerate(reader):
                    if i ==0:
                        self._company_name = row[0].replace("Growth Profitability and Financial Ratios for ", "")
        return self._company_name

    @staticmethod
    def _df_to_ws(df, wb, title=None):
        ''' Add a pandas dataframe as a new sheet '''
        ws = wb.create_sheet(title)

        bold = xls.styles.Font(bold=True)

        cell = xls.cell.cell.WriteOnlyCell(ws)
        # cell.style = 'Pandas'

        def format_first_row(row, cell):
            for c in row:
                cell.value = c
                yield cell

        rows = dataframe_to_rows(df)
        first_row = format_first_row(next(rows), cell)
        ws.append(first_row)

        for i,row in enumerate(rows):
            if i % 2 == 0:
                cell.font = xls.styles.Font(bold=True)
            else:
                cell.font = xls.styles.Font(bold=False)
            row = list(row)
            cell.value = row[0]
            row[0] = cell
            ws.append(row)
        return ws


    def get_summary_df(self):
        return self.ratios[['return-on-equity',
                            'return-on-invested-capital',
                            'book-value-per-share-%s' % self.currency,
                            'book-value-ps-growth',
                            'earnings-per-share-%s' % self.currency,
                            'earnings-ps-growth',
                            'revenue-per-share-%s' % self.currency,
                            'revenue-ps-growth',
                            'operating-cashflow-per-share-%s' % self.currency,
                            'operating-cashflow-ps-growth',
                            'pe',
                            'shares',
                            'total-debt-growth',
                            'debt-per-earnings',
                            'debt-per-bookvalue',

                            # 'long-term-debt-ps-growth',
                            ]]

    @property
    def estimated_eps_growth(self):
        return self._get_average_growth_rate('earnings-ps-growth')

    @property
    def estimated_revenue_growth(self):
        return self._get_average_growth_rate('revenue-ps-growth')

    @property
    def estimated_bookvalue_growth(self):
        return self._get_average_growth_rate('book-value-ps-growth')

    @property
    def estimated_operational_cashflow_growth(self):
        return self._get_average_growth_rate('operating-cashflow-ps-growth')

    @property
    def estimated_growth(self):
        if self._estimated_growth is None:
            growth_rates = np.array([self.estimated_eps_growth,
                                     self.estimated_revenue_growth,
                                     self.estimated_bookvalue_growth,
                                     self.estimated_operational_cashflow_growth])
            self._estimated_growth = round(np.median(growth_rates),2)
        return self._estimated_growth

    @estimated_growth.setter
    def estimated_growth(self, value):
        self._estimated_growth = value

    @property
    def estimated_bookvalue_growth(self):
        return self._get_average_growth_rate('book-value-ps-growth')


    def _get_average_growth_rate(self, key):
        average_df = self.ratios[key].dropna(axis=0, how='any')
        n = len(average_df)
        # get a logarithmic weighting function for last n years
        weights = np.log(np.arange(0, n) + self.revenue_growth_log_shift)
        # normalize werighting function
        weights = weights / np.sum(weights)
        average = np.average(average_df,  weights=weights)
        return average

    def get_estimated_eps(self, nyears=10):

        return self.ratios['earnings-per-share-%s' % self.currency].iloc[-1] * pow(1 + self.estimated_growth, nyears)

    @property
    def current_price(self):
        return self.historic_prices.iloc[-1]['Close']

    @property
    def price_projection(self):
        return self.get_price_projection(self.n_projection_years)

    @property
    def target_price(self):
        return self.get_target_price(self.n_projection_years, self.target_yield)

    @property
    def projected_dividend_earnings(self):
        if self.has_dividends:
            return sum(self.expected_dividends * pow(1+ self.projected_dividends_growth, i) for i in range(0,self.n_projection_years))
        return 0.

    def get_price_projection(self, nyears=10):
        return self.get_estimated_eps(nyears=nyears) * self.target_pe


    def get_target_price(self, nyears=10, target_yield=0.15):
        return (self.get_price_projection(nyears=nyears) + self.projected_dividend_earnings) / (1 + target_yield) ** nyears

    @property
    def current_pe(self):
        return float(self.current_price) / float(self.ratios["earnings-per-share-%s" % self.currency].iloc[-1])

    @property
    def target_pe(self):
        if self._target_pe is None:
            if 'pe' not in self.ratios:
                return -1.
            self._target_pe = min(2 * self.estimated_growth * 100,
                                   max(self.ratios['pe'])
                                  )
        return self._target_pe

    @target_pe.setter
    def target_pe(self, value):
        self._target_pe = value

    @property
    def expected_dividends(self):
        # Use the medain from the last 3 years as the default expectation
        if self._expected_dividends is None:
            if 'dividends-%s' % self.currency in self.ratios:
                self._expected_dividends = np.median(self.ratios['dividends-%s' % self.currency].iloc[-1])
            else:
                self._expected_dividends = 0.
        return self._expected_dividends

    @expected_dividends.setter
    def expected_dividends(self,value):
        self._expected_dividends = value

    @property
    def projected_dividends_growth(self):
        # Use the medain from the last 3 years as the default expectation
        if self._projected_dividends_growth is None:
            if self.has_dividends:
                if 'dividends-ps-growth' not in self.ratios:
                    self.add_dividends_ps_growth()
                self._projected_dividends_growth = self._get_average_growth_rate('dividends-ps-growth')
            else:
                self._projected_dividends_growth = 0.
        return self._projected_dividends_growth

    @projected_dividends_growth.setter
    def projected_dividends_growth(self,value):
        self._projected_dividends_growth = value

    def clear_cached_files(self):
        report_types = ('income', 'cashflow', 'balancesheet', 'ratios', 'price')
        for report_type in report_types:
            if os.path.exists(self.report_path(report_type)):
                os.remove(self.report_path(report_type))

    @property
    def income(self):
        ''' property for income report dataframe '''
        if self._income is None:
            self.income = self._load_report_csv_to_df('income')
        return self._income

    @income.setter
    def income(self, df):
        ''' property setter for income report dataframe '''
        self._income = df

    @property
    def cashflow(self):
        ''' property for cashflow report dataframe '''
        if self._cashflow is None:
            self.cashflow = self._load_report_csv_to_df('cashflow')
        return self._cashflow

    @cashflow.setter
    def cashflow(self, df):
        ''' property setter for cashflow report dataframe '''
        self._cashflow = df

    @property
    def balancesheet(self):
        ''' property for balancesheet report dataframe '''
        if self._balancesheet is None:
            self.balancesheet = self._load_report_csv_to_df('balancesheet')
        return self._balancesheet

    @balancesheet.setter
    def balancesheet(self, df):
        ''' property setter for balancesheet report dataframe '''
        self._balancesheet = df

    @property
    def ratios(self):
        ''' property for ratios report dataframe '''
        if self._ratios is None:
            self._ratios = self._load_report_csv_to_df('ratios')
            if not self._ratios.empty:
                self.add_book_value_ps_growth()
                self.add_earnings_ps_growth()
                self.add_revenue_ps_growth()
                self.add_operating_cashflow_ps_growth()
                self.add_long_term_debt_ps_growth()
                self.add_short_term_debt_ps_growth()
                self.add_total_debt()
                self.add_debt_ratios()
                self.add_pe()
                if self.has_dividends:
                    self._ratios['dividends-%s' % self.currency].fillna(0)
                self.add_dividends_ps_growth()
        return self._ratios

    @property
    def has_dividends(self):
        if 'dividends-%s' % self.currency in self._ratios:
            return True
        return False

    @ratios.setter
    def ratios(self, df):
        ''' property setter for ratios report dataframe '''
        self._ratios = df

    def report_path(self, report_type):
        ''' Get path for a report name '''
        return os.path.join(morningstar_data_path,
                            report_type+ "_%s.csv" % self.symbol)

    def add_dividends_ps_growth(self):
        if 'dividends-%s' % self.currency in self._ratios:
            self._ratios['dividends-ps-growth'] = self._ratios['dividends-%s' % self.currency].pct_change()
        self._ratios['dividends-ps-growth'] = self._ratios['shares'] * 0.

    def add_book_value_ps_growth(self):
        self._ratios['book-value-ps-growth'] = self._ratios['book-value-per-share-%s' % self.currency].pct_change()

    def add_earnings_ps_growth(self):
        self._ratios['earnings-ps-growth'] = self._ratios['earnings-per-share-%s' % self.currency].pct_change()

    def add_operating_cashflow_ps_growth(self):
        self._ratios['operating-cashflow-per-share-%s' % self.currency] = self._ratios['operating-cash-flow-%s' % self.currency] / self._ratios['shares']
        self._ratios['operating-cashflow-ps-growth'] = (self._ratios['operating-cash-flow-%s' % self.currency] / self._ratios['shares']).pct_change()

    def add_long_term_debt_ps_growth(self):
        if 'long-term-debt' in self.balancesheet:
            self._ratios['long-term-debt'] = self.balancesheet['long-term-debt']
            self._ratios['long-term-debt-ps-growth'] = (self.balancesheet['long-term-debt'] / self._ratios['shares']).pct_change()
            self._ratios['long-term-debt-ps'] = self.balancesheet['long-term-debt'] / self._ratios['shares']

    def add_short_term_debt_ps_growth(self):
        self._ratios['short-term-debt'] = self.balancesheet['short-term-debt']
        self._ratios['short-term-debt-ps-growth'] = (self.balancesheet['short-term-debt'] / self._ratios['shares']).pct_change()
        self._ratios['short-term-debt-ps'] = self.balancesheet['short-term-debt'] / self._ratios['shares']

    def add_total_debt(self):
        self._ratios['total-debt-%s' % self.currency] = self.balancesheet['short-term-debt'] + self.balancesheet['long-term-debt']
        self._ratios['total-debt-per-share-%s' % self.currency] = self._ratios['total-debt-%s' % self.currency] / self._ratios['shares']
        self._ratios['total-debt-growth'] = (self._ratios['total-debt-%s' % self.currency]).pct_change()

    def add_debt_ratios(self):
        self._ratios['debt-per-earnings'] = self._ratios['total-debt-per-share-%s' % self.currency] / self._ratios['earnings-per-share-%s' % self.currency]
        self._ratios['debt-per-bookvalue'] = self._ratios['total-debt-per-share-%s' % self.currency] / self._ratios['book-value-per-share-%s' % self.currency]


    def add_revenue_ps_growth(self):
        self._ratios['revenue-per-share-%s' % self.currency] = self._ratios['revenue-%s' % self.currency] / self._ratios['shares']
        self._ratios['revenue-ps-growth'] = (self._ratios['revenue-%s' % self.currency] / self._ratios['shares']).pct_change()

    @property
    def historic_prices(self):
        if self._historic_prices is None:
            self._historic_prices = self._load_price_csv_to_df()
        return self._historic_prices

    def add_pe(self):
        self.ratios['pe'] = self.ratios['earnings-per-share-%s' % self.currency]
        for i in self.ratios['pe'].index:
            self.ratios.loc[i,'pe'] = self.historic_prices.iloc[self.historic_prices.index.get_loc(i,method='nearest')]['Close'] / self.ratios.loc[i,'earnings-per-share-%s' % self.currency]

    def _download_morningstar_data(self, report_type):
        ''' download csv fundamentals data from morningstar '''
        # prepare url string ddependent on report type
        if report_type == "ratios":
            url = "http://financials.morningstar.com/ajax/exportKR2CSV.html?t={symbol}"
        else:
            url = "http://financials.morningstar.com/ajax/ReportProcess4CSV.html?t={symbol}&reportType={report_key}&period=12&dataType=A&order=asc&columnYear=5&number=1"
        time.sleep(0.01)
        # fill variable parts of url
        url = url.format(symbol=self.symbol,
                         report_key=self.report_key_map.get(report_type, None))
        # retrieve csv object to file
        path, request = urlretrieve(url, self.report_path(report_type))

    def _download_morningstar_pricedata(self):
        ''' download csv price data from morningstar '''
        time.sleep(0.01)
        # fill variable parts of url
        url = 'http://performance.morningstar.com/perform/Performance/stock/exportStockPrice.action?t={symbol}&pd=max&freq=d&sd=&ed=&pg=0&culture=en-US&cur={currency}'
        try:
            url = url.format(symbol=self.symbol,
                             currency=self.currency.upper())
            # retrieve csv object to file
            path, request = urlretrieve(url, self.report_path('price'))
        except:
            raise MorningStarPriceError("Unable to download price data")


    def _csv_cache_valid(self, csv_path, cache_lifetime):
        ''' Check if a csv file exists or may need an update '''
        if not os.path.exists(csv_path):
            return False
        if time.time() - os.path.getmtime(csv_path) < cache_lifetime:
            upate = False
        return True

    def _load_price_csv_to_df(self):
        csv_path = self.report_path('price')
        if not self._csv_cache_valid(csv_path, 40000):
            try:
                self._download_morningstar_pricedata()
            except MorningStarPriceError:
                self.error_log.append("Unable to load price data")
                return pd.DataFrame()
        # read csv in pandas data frame
        try:
            df = pd.read_csv(csv_path,
                             skiprows=1,
                             thousands=",",
                             index_col=0,
                             skip_blank_lines=True)
            df.index = pd.to_datetime(df.index, format='%m/%d/%Y')
            df.sort_index(ascending=True, inplace=True)
            # remove thousand separators and convert to numeric values
            df = df.apply(lambda x: pd.to_numeric(x.astype(str).str.replace(',',''), errors='coerce'))

            return df
        except pd.errors.EmptyDataError:
            self.error_log.append("No {} report found".format(report_type))
        except:
            self.error_log.append("Unable to process report: {}".format(report_type))
        return pd.DataFrame()

    def _load_report_csv_to_df(self, report_type):
        ''' load company csv reports from morningstar (from web or cache)'''

        if not self._csv_cache_valid(self.report_path(report_type), 86800):
            self._download_morningstar_data(report_type)

        try:
            # determine how many rows to skip based on report type
            skiprows = 1
            if report_type == "ratios":
                skiprows=2

            # read csv in pandas data frame
            df = pd.read_csv(self.report_path(report_type),
                        skiprows=skiprows,
                        index_col=0,
                        thousands=",",
                        skip_blank_lines=True)
            # now index = parameters columns = dates
            # rename TTM  to current date in matching format
            df = df.rename(index=str,columns={'TTM':datetime.date.today().strftime('%Y-%m')})
            # slugify the index names (parameter names)
            df.index = [slugify(i) for i in df.index]
            # transpose the table to have dates as row keys
            # now index = dates columns = parameters
            df = df.transpose()
            # convert string index objects to datetime objects
            df.index = pd.to_datetime(df.index, format='%Y-%m')
            # clean up nan columns
            df.dropna(axis=1, how='all', inplace=True)
            # remove thousand separators and convert to numeric values
            df = df.apply(lambda x: pd.to_numeric(x.astype(str).str.replace(',',''), errors='coerce'))
            # apply units from key to parameter
            for key in list(df):
                if key.endswith("-mil"):
                    df[key[:-4]] = 1e6 * df[key]
                    df.drop(key, axis=1, inplace=True)
            df = df.apply(pd.to_numeric, errors='ignore')
            return df
        except pd.errors.EmptyDataError:
            self.error_log.append("No {} report found".format(report_type))
        except:
            self.error_log.append("Unable to process report: {}".format(report_type))
        return pd.DataFrame()
