% LoadMax truck slot assignment — binary MIP (same y as scipy HiGHS).
% MiniZinc 2.10.0 (23 July 2026) + bundled HiGHS 1.15.1.

int: nVeh;
int: nLyr;
int: nPos;
int: nFull;
int: nPrtl;
int: nMtl;

set of int: VEH = 1..nVeh;
set of int: LYR = 1..nLyr;
set of int: POS = 1..nPos;
set of int: FULL = 1..nFull;
set of int: PRTL = 1..nPrtl;
set of int: MTL = 1..nMtl;

array[1..nLyr] of int: lyr_ord;

int: first_plt_n;
int: max_plt_n;
int: max_lyr_n;
int: unique_sku_n;
int: sku_scale;

float: max_wt;
float: max_front_wt;
float: max_rear_wt;
float: max_ht;
float: door_ht;
float: allow_ht;
float: fb_tol;
float: ss_tol;
float: incentive;
float: sku_pen_coeff;

bool: use_axles;
bool: use_compact;
bool: use_balance_fb;
bool: use_balance_ss;
bool: use_crushing;
bool: use_vdr;
bool: use_warm_start;

set of int: door_posn;
array[POS] of float: front_coeff;
array[POS] of float: rear_coeff;
array[POS] of 0..1: is_front;
array[POS] of 0..1: is_rear;
array[POS] of 0..1: is_left;
array[POS] of 0..1: is_right;

array[FULL] of float: weight_full;
array[PRTL] of float: weight_prtl;
array[FULL] of float: height_full;
array[PRTL] of float: height_prtl;
array[FULL] of int: mtl_of_full;
array[PRTL] of int: mtl_of_prtl;
array[FULL] of int: demand_full;
array[PRTL] of int: demand_prtl;
array[FULL] of float: max_prior_full;
array[PRTL] of float: max_prior_prtl;
array[VEH, FULL] of float: fill_full;
array[VEH, PRTL] of float: fill_prtl;
array[FULL] of 0..1: g2_full;
array[PRTL] of 0..1: g2_prtl;
array[FULL] of 0..1: g3_full;
array[PRTL] of 0..1: g3_prtl;
array[FULL] of 0..1: g4_full;
array[PRTL] of 0..1: g4_prtl;

array[VEH, LYR, POS, FULL] of var 0..1: y_full;
array[VEH, LYR, POS, PRTL] of var 0..1: y_prtl;
array[VEH, LYR, POS, FULL] of 0..1: warm_y_full;
array[VEH, LYR, POS, PRTL] of 0..1: warm_y_prtl;
array[VEH, LYR, POS] of 0..nFull: warm_slot_full;
array[VEH, LYR, POS] of 0..nPrtl: warm_slot_prtl;
array[VEH, LYR, POS] of 0..1: warm_occ;
array[VEH, LYR, POS] of float: warm_wt;
array[VEH, LYR, POS] of float: warm_ht;

% Compact output: pallet index 0 = empty.
array[VEH, LYR, POS] of var 0..nFull: slot_full :: output;
array[VEH, LYR, POS] of var 0..nPrtl: slot_prtl :: output;

constraint forall(v in VEH, l in LYR, p in POS)(
  sum(f in FULL)(y_full[v, l, p, f]) + sum(r in PRTL)(y_prtl[v, l, p, r]) <= 1
  /\ slot_full[v, l, p] = sum(f in FULL)(f * y_full[v, l, p, f])
  /\ slot_prtl[v, l, p] = sum(r in PRTL)(r * y_prtl[v, l, p, r])
);

array[VEH, LYR, POS] of var 0..1: occ;
constraint forall(v in VEH, l in LYR, p in POS)(
  occ[v, l, p] = sum(f in FULL)(y_full[v, l, p, f]) + sum(r in PRTL)(y_prtl[v, l, p, r])
);

constraint forall(f in FULL)(
  sum(v in VEH, l in LYR, p in POS)(y_full[v, l, p, f]) <= demand_full[f]
);
constraint forall(r in PRTL)(
  sum(v in VEH, l in LYR, p in POS)(y_prtl[v, l, p, r]) <= demand_prtl[r]
);

array[VEH, LYR, POS] of var 0.0..max_wt: wt;
array[VEH, LYR, POS] of var 0.0..max_ht: ht;
constraint forall(v in VEH, l in LYR, p in POS)(
  wt[v, l, p] = sum(f in FULL)(y_full[v, l, p, f] * weight_full[f])
              + sum(r in PRTL)(y_prtl[v, l, p, r] * weight_prtl[r])
  /\ ht[v, l, p] = sum(f in FULL)(y_full[v, l, p, f] * height_full[f])
                 + sum(r in PRTL)(y_prtl[v, l, p, r] * height_prtl[r])
);

constraint forall(v in VEH)(
  sum(l in LYR, p in POS)(wt[v, l, p]) <= max_wt
);

constraint forall(v in VEH, p in POS)(
  sum(l in LYR)(ht[v, l, p])
    <= (if p in door_posn then door_ht else max_ht endif) - allow_ht
);

constraint forall(v in VEH, i in 1..nLyr - 1, p in POS)(
  sum(r in PRTL)(y_prtl[v, lyr_ord[i], p, r]) + occ[v, lyr_ord[i + 1], p] <= 1
);

constraint if use_crushing then
  forall(v in VEH, i in 1..nLyr - 1, p in POS)(
    wt[v, lyr_ord[i], p]
      >= sum(j in i + 1..nLyr)(wt[v, lyr_ord[j], p])
  )
else true endif;

constraint if use_compact then
  forall(v in VEH, l in LYR, p in POS)(
    (p < first_plt_n /\ p + 1 in POS -> occ[v, l, p] <= occ[v, l, p + 1])
    /\ (p > first_plt_n /\ p - 1 in POS -> occ[v, l, p] <= occ[v, l, p - 1])
    /\ (p <= first_plt_n /\ p mod 2 = 0 /\ p - 1 in POS
        -> occ[v, l, p] = occ[v, l, p - 1])
  )
  /\ forall(v in VEH, i in 1..nLyr - 1 where first_plt_n in POS)(
      let { int: l = lyr_ord[i + 1]; int: lp = lyr_ord[i]; } in
      (1 in POS -> occ[v, l, first_plt_n] <= occ[v, lp, 1])
      /\ (max_plt_n in POS -> occ[v, l, first_plt_n] <= occ[v, lp, max_plt_n])
    )
else true endif;

constraint if use_axles then
  forall(v in VEH)(
    sum(l in LYR, p in POS)(wt[v, l, p] * front_coeff[p]) <= max_front_wt
    /\ sum(l in LYR, p in POS)(wt[v, l, p] * rear_coeff[p]) <= max_rear_wt
  )
else true endif;

constraint if use_balance_fb then
  forall(v in VEH)(
    let {
      var float: front = sum(l in LYR, p in POS)(wt[v, l, p] * is_front[p]),
      var float: rear = sum(l in LYR, p in POS)(wt[v, l, p] * is_rear[p])
    } in
    front <= (1.0 + fb_tol) * rear
    /\ front >= (1.0 - fb_tol) * rear
  )
else true endif;

constraint if use_balance_ss then
  forall(v in VEH)(
    let {
      var float: left = sum(l in LYR, p in POS)(wt[v, l, p] * is_left[p]),
      var float: right = sum(l in LYR, p in POS)(wt[v, l, p] * is_right[p])
    } in
    left <= (1.0 + ss_tol) * right
    /\ left >= (1.0 - ss_tol) * right
  )
else true endif;

array[VEH, MTL] of var 0..sku_scale: mtl_count;
array[VEH, MTL] of var 0..1: y_mtl;
constraint forall(v in VEH, m in MTL)(
  mtl_count[v, m] =
    sum(l in LYR, p in POS, f in FULL where mtl_of_full[f] = m)(y_full[v, l, p, f])
    + sum(l in LYR, p in POS, r in PRTL where mtl_of_prtl[r] = m)(y_prtl[v, l, p, r])
  /\ mtl_count[v, m] <= sku_scale * y_mtl[v, m]
);

var 0..nVeh * nMtl: exceed_mtl_n;
constraint exceed_mtl_n >= sum(v in VEH, m in MTL)(y_mtl[v, m]) - unique_sku_n;

array[FULL] of var float: y_prior_full;
constraint forall(f in FULL)(
  y_prior_full[f] >= 0.0
  /\ y_prior_full[f] <= max_prior_full[f]
  /\ y_prior_full[f] <= sum(v in VEH, l in LYR, p in POS)(y_full[v, l, p, f])
);

array[PRTL] of var float: y_prior_prtl;
constraint forall(r in PRTL)(
  y_prior_prtl[r] >= 0.0
  /\ y_prior_prtl[r] <= max_prior_prtl[r]
  /\ y_prior_prtl[r] <= sum(v in VEH, l in LYR, p in POS)(y_prtl[v, l, p, r])
);

int: ground = lyr_ord[1];
constraint if use_vdr then
  forall(v in VEH, l in LYR, p in POS, f in FULL where g2_full[f] = 1)(
    l != ground -> y_full[v, l, p, f] = 0
  )
  /\ forall(v in VEH, l in LYR, p in POS, r in PRTL where g2_prtl[r] = 1)(
    l != ground -> y_prtl[v, l, p, r] = 0
  )
  /\ forall(v in VEH, l in LYR, p in POS, f in FULL where g4_full[f] = 1)(
    l != ground -> y_full[v, l, p, f] = 0
  )
  /\ forall(v in VEH, l in LYR, p in POS, r in PRTL where g4_prtl[r] = 1)(
    l != ground -> y_prtl[v, l, p, r] = 0
  )
  /\ forall(v in VEH, i in 1..nLyr - 1, p in POS)(
      sum(f in FULL)(g3_full[f] * y_full[v, lyr_ord[i], p, f])
      + sum(r in PRTL)(g3_prtl[r] * y_prtl[v, lyr_ord[i], p, r])
      + occ[v, lyr_ord[i + 1], p] <= 1
    )
  /\ forall(v in VEH, i in 1..nLyr - 1, p in POS)(
      sum(f in FULL)(g4_full[f] * y_full[v, lyr_ord[i], p, f])
      + sum(r in PRTL)(g4_prtl[r] * y_prtl[v, lyr_ord[i], p, r])
      + occ[v, lyr_ord[i + 1], p] <= 1
    )
else true endif;

var float: obj :: output;
constraint obj =
  sum(v in VEH, l in LYR, p in POS, f in FULL)(y_full[v, l, p, f] * fill_full[v, f])
  + sum(v in VEH, l in LYR, p in POS, r in PRTL)(y_prtl[v, l, p, r] * fill_prtl[v, r])
  + incentive * (sum(f in FULL)(y_prior_full[f]) + sum(r in PRTL)(y_prior_prtl[r]))
  - sku_pen_coeff * exceed_mtl_n;

% MiniZinc 2.10 HiGHS/SCIP warm_start. restart_none is a no-op ann when cold.
ann: warm_ann =
  if use_warm_start then
    warm_start_array([
      warm_start(array1d(y_full), array1d(warm_y_full)),
      warm_start(array1d(y_prtl), array1d(warm_y_prtl)),
      warm_start(array1d(slot_full), array1d(warm_slot_full)),
      warm_start(array1d(slot_prtl), array1d(warm_slot_prtl)),
      warm_start(array1d(occ), array1d(warm_occ)),
      warm_start(array1d(wt), array1d(warm_wt)),
      warm_start(array1d(ht), array1d(warm_ht))
    ])
  else restart_none endif;

solve :: warm_ann maximize obj;
