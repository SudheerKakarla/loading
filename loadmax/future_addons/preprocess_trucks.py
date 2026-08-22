import os

length_to_foot = {
    'in': 1 / 12,
    'IN': 1 / 12,
    'ft': 1,
    'FT': 1,
    'm': 3.28084,
    'M': 3.28084,
    'cm': 0.0328084,
    'CM': 0.0328084,
    'mm': 0.00328084,
    'MM': 0.00328084
}


def truck_preprocess(input_file):
    """The data preprocessing wrapper function for truck loading.

    Args:
        app (LoadMaxApp): The application instance.
    """

    # read data from input files
    # read_input(app)
    dfs = read_input(input_file)

    print("Preprocessing input data...")

    # preprocess demand data

    demand_df = dfs['Data'].copy()

    # rename columns
    demand_df.columns = ['batch', 'mtl', 'mkt', 'priority', 'amount']

    # drop empty rows
    demand_df = demand_df[demand_df['amount'] > 0]

    # modify SKU: string with 9 leading 0's
    demand_df['mtl'] = '0' * 9 + demand_df['mtl'].astype(int).astype(
        str)

    # read master data
    sql_df = read_master_data()

    # check if all SKU are available; if not, look for alternate SKU
    lookup_alter_sku(sql_df, demand_df['mtl'].drop_duplicates().values)

    # merge demand_df and sql_df
    input_df = pd.merge(demand_df, sql_df, on='mtl')
    #input_df.drop_duplicates(subset=['batch', 'mtl', 'mkt'], keep='first', inplace=True)
    input_df = input_df[input_df.cse_wt.notnull()]
    input_df.drop_duplicates(subset=['batch', 'mtl', 'mkt'], keep='last', inplace=True)
    
    # obtain column 'eaches/plt'
    input_df['eaches/plt'] = input_df['pcs/cse'] * input_df['cses/plt']

    # extract pallet specifications
    unit = dfs['Options']['Input in Cases or Pallets?']
    plt_base_wt = dfs['Options']['Pallet Weight [lb]']

    # transfer height: from inch to foot
    plt_base_ht = (dfs['Options']['Pallet Height [in]'] / 12)

    # generate 'plt_n' and 'prtl_frac' columns
    if unit == 'Cases':
        input_df['plt_n'] = input_df['amount'] // input_df['cses/plt']
        input_df['plt_n'] = input_df['plt_n'].astype(int)
        input_df['prtl_frac'] = input_df['amount'] / input_df['cses/plt'] - \
            input_df['plt_n']
    elif unit == 'Pallets':
        input_df['plt_n'] = input_df['amount'].apply(math.floor)
        input_df['prtl_frac'] = input_df['amount'] - input_df['plt_n']
    else:
        # TODO(Pengfei) change this to errors in data validation
        raise ValueError(f"Cargo unit {unit} is invalid. "
                         f"It must be 'Cases' or 'Pallets'.")

    # create column to indicate if it has partial pallets
    input_df['prtl'] = input_df.apply(
        lambda c: 1 if c['prtl_frac'] > 0 else 0, axis=1)

    # change column data type
    input_df['cse_wt'] = input_df['cse_wt'].astype(float)
    input_df['cse_ht'] = input_df['cse_ht'].astype(float)

    # calculate weight/partial weight
    input_df['wt'] = (input_df['cse_wt'].astype(float) * input_df['cses/plt']) + plt_base_wt
    input_df['prtl_wt'] = ((input_df['wt'] - plt_base_wt) * input_df['prtl_frac']) + plt_base_wt

    # calculate height
    # assume pallet area is 40'' x 48''
    input_df['ht'] = input_df.apply(lambda c: (c['cse_ht'] * c['lyrs/plt'] * length_to_foot[c['size_unit']]) + plt_base_ht, axis=1)

    def calc_partial_height(c):
        """Helper function to calculate partial height."""
        if c['cses/plt'] * c['lyrs/plt'] > 0:
            return c['ht'] * math.ceil(c['prtl_frac'] * c['lyrs/plt']) / c['lyrs/plt'] + plt_base_ht
        else:
            return ((c['ht'] - plt_base_ht) * c['prtl_frac']) + plt_base_ht

    # calculate partial height
    input_df['prtl_ht'] = input_df.apply(calc_partial_height, axis=1)

    # add 1 to index so that it starts from 1
    input_df.index += 1

    # remove leading 0s from materials
    input_df['mtl'] = input_df['mtl'].apply(lambda x: x[9:])

    # split full and partial pallets
    input_df = split_full_prtl_plts(input_df)

    # store (temporarily in LOAD model) into app
    app_input_df = input_df.astype({
        'cse_ht': float, 'cse_wt': float, 'cses/plt': int, 'lyrs/plt': int,
        'amount': int, 'pcs/cse': int, 'eaches/plt': int,
    }).rename(columns={
        'batch': 'Batch', 'mtl': 'Material', 'mkt': 'Customer',
        'desc': 'Description', 'lyrs/plt': 'Layers/Pallet',
        'eaches/plt': 'Pcs/Pallet', 'plt_n': 'Pallet',
        'prtl_frac': 'Partial Pallet Fraction', 'prtl': 'Partial Pallets',
        'wt': 'Pallet Weight', 'prtl_wt': 'Partial Pallet Weight',
        'ht': 'Pallet Height', 'prtl_ht': 'Partial Pallet Height'
    })

    ########################################################
    # add unused columns
    app_input_df['STO Number'] = ''
    app_input_df['Brand'] = ''

    # updating balance/priority pallets
    app_input_df['Balance Pallets'] = ''

    def calc_balance_pallets(c):
        """Helper function to calculate balance pallets"""
        if c['priority'] == 'YES':
            if c['Pallet'] >= c['Partial Pallets']:
                return c['Pallet']
            else:
                return c['Partial Pallets']
        else:
            return 0
    app_input_df['Balance Pallets'] = app_input_df.apply(
        calc_balance_pallets, axis=1)

    # drop all duplicate rows
    app_input_df.drop_duplicates(subset=None, keep="first", inplace=True)

    # load market index set
    app_mkt_id = pd.Index(input_df['mkt'].drop_duplicates().values)
    print("market ids:", app_mkt_id)

    # write data to CSV file
    # input_df.to_csv(outputfile_input_df_data)
    app_input_df.to_csv(outputfile_app_input_df_data, index=False)

    # print("Type of app_input_df:", type(app_input_df))
    #return app_mkt_id

def read_input(input_file):
    print("############################################")
    """Read truck input file.

    Raises:
        InputTemplateVersionError when the file version is incompatible.
    """

    # app.print_w_time(f"Reading from {app.input_file}...")
    print("Reading data from LoadMax Input Template (Truck)..")

    # read all tables from Excel
    dfs = pd.read_excel(input_file, sheet_name=[
                        'Data', 'Limits', 'Config', 'Options'])

    # read 'Config' tab
    dfs['Config'] = dfs['Config'].loc[:, 'Version':'Height at Door'].dropna(
        how='all')

    # verify input file version
    input_f_version = dfs['Config'].loc[0, 'Version']
    print("Printing file version....", input_f_version)
    # if input_f_version == 'JUL-08-2021':
    #     pass
    # else:
    #     raise InputTemplateVersionError(input_f_version)

    # read 'Options' tab
    dfs['Options'] = dfs['Options'][['Unnamed: 1', 'Unnamed: 2']].dropna(
        how='all').set_index('Unnamed: 1')['Unnamed: 2']

    # read 'Data' tab
    data_columns = ['Order Number', 'SKU', 'Ship To', 'Priority',
                    'Pallets or Cases (Set option on previous tab)']
    dfs['Data'] = dfs['Data'][data_columns].dropna(how='all')

    # read 'Limits' tab
    limits_columns = ['Ship-To', 'Max Cargo Wgt [lb]',
                      'Front Axle Max Cargo Wgt [lb]',
                      'Rear Axle Max Cargo Wgt [lb]']
    dfs['Limits'] = dfs['Limits'][limits_columns].dropna(how='all').set_index(
        'Ship-To')

    # store all data into truck_dfs (temporarily in LOAD mode)
    # app.truck_dfs = dfs
    # print(dfs)
    print("Printing data read from excel input template...")
    dfs['Data'].to_csv(outputfile_data)
    dfs['Config'].to_csv(outputfile_config)
    dfs['Limits'].to_csv(outputfile_limits)
    dfs['Options'].to_csv(outputfile_options)

    return dfs


def read_master_data():
    print("############################################")
    """Read master data from database."""
    print("Reading master data from the SQL database..")

    # SQL credentials come from the environment. Never commit a password.
    uid = os.environ.get("LOADMAX_SQL_UID", "sacondiscreadonly")
    pwd = os.environ.get("LOADMAX_SQL_PWD")
    if not pwd:
        raise RuntimeError(
            "Set LOADMAX_SQL_PWD (and optionally LOADMAX_SQL_UID, "
            "LOADMAX_SQL_SERVER, LOADMAX_SQL_DATABASE) before reading master data."
        )
    server = os.environ.get(
        "LOADMAX_SQL_SERVER", "tcp:scprod.database.windows.net,1433"
    )
    database = os.environ.get("LOADMAX_SQL_DATABASE", "scprod")
    conn = pyodbc.connect(
        "Driver={ODBC Driver 17 for SQL Server};"
        f"Server={server};"
        f"Database={database};"
        f"Uid={uid};"
        f"Pwd={pwd};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    # SQL command (originally from Mosel code)
    sql = r'SELECT matl_num, matl_desc, gross_weight_cs, height_cs, ' \
          r'stack_factor, floor(lyr_per_pal), floor(itm_per_pal), ' \
          r'floor(ea_per_cs), unit_dimension_cs, weight_unit_cs  ' \
          r'FROM conwarehouse.matl_mstr_aggregated'
    # read into sql_df
    sql_df = pd.read_sql(sql, conn)

    # update column labels
    sql_df.columns = ['mtl', 'desc', 'cse_wt', 'cse_ht', 'stk_factor',
                      'lyrs/plt', 'cses/plt', 'pcs/cse', 'size_unit', 'wt_unit']

    # load into the app
    # app.sql_df = sql_df
    print("Printing data read from SQL database...")
    sql_df.to_csv(outputfile_master)
    # print(sql_df)
    #
    # print("Type of data read from database:", type(sql_df))

    return sql_df


def lookup_alter_sku(sql_df, mtl_list):
    """Look up alternate SKU when there is no exact SKU match in the master
    data.

    When no exact match, look for SKUs sharing last 9 - last 3 digits
    (the last 2 digits are mod code). When there is multiple matches, choose
    the one with the largest numeric.

    Args:
        app (LoadMaxApp): The application instance.
        mtl_list (list): The list of materials in demand.

    Raises:
        ValueError when there is no similar SKU match in the master data.
    """

    # add column 'alter_sku'
    sql_df['alter_sku'] = ''

    # check each SKU
    for sku in mtl_list:
        # check if sku is in the master data
        if sku not in sql_df['mtl'].values:

            # get the last 9-last 3 digits
            sku_wo_mod = sku[-9:-2]

            # get the corresponding SKUs in sql sharing those digits
            sql_skus = [i for i in sql_df['mtl'].values if
                        i[-9:-2] == sku_wo_mod]
            if len(sql_skus) == 0:
                raise ValueError(f"No matching data for SKU {sku}")

            # choose the numerically largest one
            max_sql_sku_int = max([int(i) for i in sql_skus])
            max_sql_sku = '0' * (18 - len(str(max_sql_sku_int))) + str(
                max_sql_sku_int)

            # replace max_sql_sku with sku in sql_df; add max_sql_sku to column
            # 'alter_sku'
            print(f"        SKU {sku} data replaced with "
                  f"SKU {max_sql_sku}")
            idx = sql_df.index[sql_df['mtl'] == max_sql_sku]
            sql_df.loc[idx, 'mtl'] = sku
            sql_df.loc[idx, 'alter_sku'] = str(max_sql_sku_int)


def split_full_prtl_plts(df):
    """Split rows with both full pallets and partial pallets into multiple rows,
    one corresponding to only full pallets, and one corresponding to only
    partial pallets.

    Args:
        df (pd.DataFrame): The table to be processed.

    Returns:
        Processed table.
    """

    # generate two copies of df
    df_full = df.copy()
    df_prtl = df.copy()

    # remove partial pallet info on df_full
    df_full['prtl'] = 0
    df_full['prtl_frac'] = 0

    # remove full pallet info on df_full
    df_prtl['plt_n'] = 0

    # drop all zero rows
    df_full = df_full[df_full['plt_n'] > 0]
    df_prtl = df_prtl[df_prtl['prtl_frac'] > 0]

    # combine two dfs
    df = pd.concat([df_full, df_prtl], ignore_index=True)

    # update index
    df.index += 1

    return df


# execute pre-processing
#truck_preprocess(inputFile_Data)