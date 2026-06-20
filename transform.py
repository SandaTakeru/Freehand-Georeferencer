# -*- coding: utf-8 -*-
'''Computes the transform matrix from control points (GCPs).

A least-squares fit (NumPy) returns a 2x3 affine matrix M:
  x' = M[0,0]*x + M[0,1]*y + M[0,2]
  y' = M[1,0]*x + M[1,1]*y + M[1,2]

Only two transform modes:
  - 'helmert': rotation + translation (+ optional uniform scale).
               lock_scale=True keeps scale=1 (rigid).
  - 'affine' : rotation + translation + scale + shear (full 6 parameters).

When there are too few points it falls back gradually:
  1 point -> translation only / 2 points -> Helmert-equivalent /
  3+ points -> the full degrees of freedom of the selected mode.
'''

import numpy as np


def _identity():
    return np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])


def _translation(src, dst):
    # Translation only, from the centroid difference of the point pairs.
    t = (dst - src).mean(axis=0)
    return np.array([[1.0, 0.0, t[0]], [0.0, 1.0, t[1]]])


def _rigid(src, dst):
    # Fixed scale (rigid): optimal rotation via orthogonal Procrustes.
    mu_s = src.mean(axis=0)
    mu_d = dst.mean(axis=0)
    S = src - mu_s
    D = dst - mu_d
    H = S.T @ D
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, d]) @ U.T
    t = mu_d - R @ mu_s
    return np.array([[R[0, 0], R[0, 1], t[0]], [R[1, 0], R[1, 1], t[1]]])


def _similarity(src, dst):
    # Helmert (similarity): least-squares solve of
    # x' = a*x - b*y + tx, y' = b*x + a*y + ty.
    A = []
    B = []
    for (x, y), (X, Y) in zip(src, dst):
        A.append([x, -y, 1.0, 0.0])
        B.append(X)
        A.append([y, x, 0.0, 1.0])
        B.append(Y)
    p, _res, _rank, _sv = np.linalg.lstsq(np.array(A), np.array(B), rcond=None)
    a, b, tx, ty = p
    return np.array([[a, -b, tx], [b, a, ty]])


def _affine(src, dst):
    # Affine: least-squares solve per component of
    # x' = a*x + b*y + c, y' = d*x + e*y + f.
    A = np.column_stack([src[:, 0], src[:, 1], np.ones(len(src))])
    px, _r1, _k1, _s1 = np.linalg.lstsq(A, dst[:, 0], rcond=None)
    py, _r2, _k2, _s2 = np.linalg.lstsq(A, dst[:, 1], rcond=None)
    return np.array([[px[0], px[1], px[2]], [py[0], py[1], py[2]]])


def compute_matrix(src, dst, mode='helmert', lock_scale=False):
    '''Computes a 2x3 transform matrix from the control points.

    src, dst : Nx2 arrays (old coordinates / new coordinates).
    '''
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    n = len(src)
    if n == 0:
        return _identity()
    if n == 1:
        return _translation(src, dst)
    if mode == 'affine':
        if n >= 3:
            return _affine(src, dst)
        # Affine is underdetermined with 2 points, so fall back to Helmert.
        return _similarity(src, dst)
    # helmert
    if lock_scale:
        return _rigid(src, dst)
    return _similarity(src, dst)


def apply_matrix(M, pts):
    '''Applies the 2x3 matrix to an Nx2 point set and returns Nx2.'''
    pts = np.asarray(pts, dtype=float).reshape(-1, 2)
    return (M[:, :2] @ pts.T + M[:, 2:3]).T


def residuals(M, src, dst):
    '''Returns the per-point residuals (dst - transformed src) as Nx2.'''
    src = np.asarray(src, dtype=float).reshape(-1, 2)
    dst = np.asarray(dst, dtype=float).reshape(-1, 2)
    return dst - apply_matrix(M, src)


def error_stats(M, src, dst):
    '''Returns residual statistics.

    Returned dict:
      per_point : Nx3 (ex, ey, mag)
      rms       : overall RMS (based on mag)
      std       : standard deviation of mag
    '''
    res = residuals(M, src, dst)
    if len(res) == 0:
        return {'per_point': np.zeros((0, 3)), 'rms': 0.0, 'std': 0.0}
    mag = np.sqrt(res[:, 0] ** 2 + res[:, 1] ** 2)
    per_point = np.column_stack([res[:, 0], res[:, 1], mag])
    rms = float(np.sqrt(np.mean(res[:, 0] ** 2 + res[:, 1] ** 2)))
    std = float(np.std(mag))
    return {'per_point': per_point, 'rms': rms, 'std': std}


def to_qtransform(M):
    '''Converts the 2x3 matrix to a QTransform.

    QTransform(m11, m12, m21, m22, dx, dy) means
      x' = m11*x + m21*y + dx
      y' = m12*x + m22*y + dy
    which maps to this module's M as below.
    '''
    from qgis.PyQt.QtGui import QTransform
    return QTransform(
        M[0, 0], M[1, 0],
        M[0, 1], M[1, 1],
        M[0, 2], M[1, 2],
    )
